import json
import sqlite3
from fractions import Fraction

from bench.snapshot import (
    BenchSnapshot, RevealedResult, bench_piece_payload, bench_snapshot_from_payload,
    build_bench_snapshot,
)
from prospective.pieces import (
    ProspectiveForecast, ProspectiveResults, build_forecast_piece, build_results_piece,
    canonical_payload_text, forecast_piece_from_payload, forecast_piece_payload,
    prospective_closeout_id, results_piece_from_payload, results_piece_payload,
)
from sqlite_tx import write_transaction
from study.identity import validate_identifier
from study.roster import Roster


class BenchStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.initialize()

    def initialize(self) -> None:
        with write_transaction(self.connection):
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS bench_results (
                    batch_id TEXT NOT NULL,
                    specimen_id TEXT NOT NULL,
                    outcome INTEGER NOT NULL CHECK (outcome IN (0, 1)),
                    source TEXT NOT NULL,
                    revealed INTEGER NOT NULL DEFAULT 0 CHECK (revealed IN (0, 1)),
                    PRIMARY KEY (batch_id, specimen_id)
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS bench_closeouts (
                    batch_id TEXT PRIMARY KEY NOT NULL,
                    payload TEXT NOT NULL
                )
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS bench_locks (
                    batch_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    specimen_id TEXT NOT NULL,
                    probability TEXT NOT NULL,
                    source TEXT NOT NULL,
                    binding TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('locked', 'revealed', 'already_locked')),
                    payload TEXT NOT NULL,
                    PRIMARY KEY (batch_id, request_id)
                )
            ''')
            self.connection.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS bench_locks_one_success
                ON bench_locks (batch_id, specimen_id) WHERE status = 'locked'
            ''')
            self.connection.execute('''
                CREATE TABLE IF NOT EXISTS prospective_closeouts (
                    batch_id TEXT PRIMARY KEY NOT NULL,
                    payload TEXT NOT NULL
                )
            ''')

    def save_result(self, batch_id: str, result: RevealedResult) -> RevealedResult:
        key = (batch_id, result.specimen_id)
        with write_transaction(self.connection):
            row = self._find_result(key)
            if row is None:
                self.connection.execute('''
                    INSERT INTO bench_results (batch_id, specimen_id, outcome, source)
                    VALUES (?, ?, ?, ?)
                ''', (*key, result.outcome, result.source))
            else:
                outcome, source, revealed = row
                if revealed:
                    original = RevealedResult(result.specimen_id, outcome, source)
                    if result != original:
                        raise ValueError('revealed outcome and source are fixed')
                    return original
                self.connection.execute('''
                    UPDATE bench_results SET outcome = ?, source = ?
                    WHERE batch_id = ? AND specimen_id = ?
                ''', (result.outcome, result.source, *key))
        return result

    def mark_revealed(self, batch_id: str, specimen_id: str) -> RevealedResult:
        key = (batch_id, specimen_id)
        with write_transaction(self.connection):
            row = self._find_result(key)
            if row is None:
                raise ValueError('specimen has no recorded result')
            outcome, source, revealed = row
            if not revealed:
                self.connection.execute('''
                    UPDATE bench_results SET revealed = 1
                    WHERE batch_id = ? AND specimen_id = ?
                ''', key)
        return RevealedResult(specimen_id, outcome, source)

    def save_or_recall_closeout(self, roster: Roster) -> BenchSnapshot:
        key = (roster.batch_id,)
        with write_transaction(self.connection):
            saved = self.connection.execute(
                'SELECT payload FROM bench_closeouts WHERE batch_id = ?', key
            ).fetchone()
            if saved is not None:
                original = bench_snapshot_from_payload(json.loads(saved[0]))
                if original.roster != roster:
                    raise ValueError('closeout already has a different roster')
                return original
            rows = self.connection.execute('''
                SELECT specimen_id, outcome, source FROM bench_results
                WHERE batch_id = ? AND revealed = 1
            ''', key).fetchall()
            snapshot = build_bench_snapshot(roster, [RevealedResult(*row) for row in rows])
            payload = json.dumps(bench_piece_payload(snapshot), sort_keys=True, separators=(',', ':'))
            self.connection.execute(
                'INSERT INTO bench_closeouts (batch_id, payload) VALUES (?, ?)',
                (roster.batch_id, payload),
            )
            return snapshot

    def _find_result(self, key: tuple[str, str]) -> tuple[int, str, int] | None:
        return self.connection.execute('''
            SELECT outcome, source, revealed FROM bench_results
            WHERE batch_id = ? AND specimen_id = ?
        ''', key).fetchone()

    # -- prospective locks -------------------------------------------------
    def lock_forecast(
        self, roster: Roster, request_id: str, specimen_id: str,
        probability: Fraction, source: str, *, custody=None,
    ) -> dict:
        piece = build_forecast_piece(roster, request_id, specimen_id, probability, source)
        payload = canonical_payload_text(forecast_piece_payload(piece))
        probability_text = f'{piece.probability.numerator}/{piece.probability.denominator}'
        with write_transaction(self.connection):
            existing = self._find_lock(roster.batch_id, piece.request_id)
            if existing is not None:
                stored_specimen, stored_probability, stored_source, stored_binding, status = existing
                if (stored_specimen != piece.specimen_id
                        or _fraction_text(stored_probability) != probability_text
                        or stored_source != piece.source
                        or stored_binding != piece.binding):
                    raise ValueError('request identifier already decided for different content')
                return _decision(roster.batch_id, piece.request_id, piece.specimen_id, status)

            if self._is_revealed(roster.batch_id, piece.specimen_id):
                status = 'revealed'
            elif self._find_successful_lock(roster.batch_id, piece.specimen_id) is not None:
                status = 'already_locked'
            else:
                status = 'locked'
            if status == 'locked' and custody is not None:
                # The exact body is durably held elsewhere before the lock
                # first takes effect; a custody failure aborts the lock.
                custody(piece)
            self.connection.execute('''
                INSERT INTO bench_locks
                    (batch_id, request_id, specimen_id, probability, source, binding, status, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (roster.batch_id, piece.request_id, piece.specimen_id, probability_text,
                  piece.source, piece.binding, status, payload))
            return _decision(roster.batch_id, piece.request_id, piece.specimen_id, status)

    def load_locked_forecast(self, batch_id: str, request_id: str) -> ProspectiveForecast:
        key = (validate_identifier(batch_id, 'batch_id'), validate_identifier(request_id, 'request_id'))
        existing = self._find_lock(*key)
        if existing is None:
            raise KeyError(key)
        payload = self.connection.execute('''
            SELECT payload FROM bench_locks WHERE batch_id = ? AND request_id = ?
        ''', key).fetchone()[0]
        if existing[4] != 'locked':
            reason = ('the specimen was already revealed' if existing[4] == 'revealed'
                      else 'another request already holds the lock')
            raise ValueError(f'request {request_id!r} was refused: {reason}')
        return forecast_piece_from_payload(json.loads(payload))

    def _is_revealed(self, batch_id: str, specimen_id: str) -> bool:
        row = self.connection.execute('''
            SELECT revealed FROM bench_results WHERE batch_id = ? AND specimen_id = ?
        ''', (batch_id, specimen_id)).fetchone()
        return bool(row is not None and row[0])

    def _find_lock(self, batch_id: str, request_id: str):
        return self.connection.execute('''
            SELECT specimen_id, probability, source, binding, status FROM bench_locks
            WHERE batch_id = ? AND request_id = ?
        ''', (batch_id, request_id)).fetchone()

    def _find_successful_lock(self, batch_id: str, specimen_id: str):
        return self.connection.execute('''
            SELECT request_id, probability, source, binding FROM bench_locks
            WHERE batch_id = ? AND specimen_id = ? AND status = 'locked'
        ''', (batch_id, specimen_id)).fetchone()

    def save_or_recall_prospective_closeout(self, roster: Roster) -> ProspectiveResults:
        key = (roster.batch_id,)
        with write_transaction(self.connection):
            saved = self.connection.execute(
                'SELECT payload FROM prospective_closeouts WHERE batch_id = ?', key
            ).fetchone()
            if saved is not None:
                original = results_piece_from_payload(json.loads(saved[0]))
                if original.roster != roster:
                    raise ValueError('prospective closeout already has a different roster')
                return original
            revealed = {
                row[0]: (row[1], row[2]) for row in self.connection.execute('''
                    SELECT specimen_id, outcome, source FROM bench_results
                    WHERE batch_id = ? AND revealed = 1
                ''', key).fetchall()
            }
            missing = [name for name in roster.specimens if name not in revealed]
            if missing:
                raise ValueError(f'batch is not fully revealed: {missing!r}')
            rows = []
            for name in roster.specimens:
                outcome, source = revealed[name]
                lock = self._find_successful_lock(roster.batch_id, name)
                rows.append({
                    'specimen_id': name,
                    'outcome': outcome,
                    'source': source,
                    'lock': None if lock is None else {'request_id': lock[0], 'binding': lock[3]},
                })
            snapshot = build_results_piece(
                roster, prospective_closeout_id(roster.batch_id), rows,
            )
            payload = canonical_payload_text(results_piece_payload(snapshot))
            self.connection.execute(
                'INSERT INTO prospective_closeouts (batch_id, payload) VALUES (?, ?)',
                (roster.batch_id, payload),
            )
            return snapshot


def _fraction_text(text: str) -> str:
    value = Fraction(text)
    return f'{value.numerator}/{value.denominator}'


def _decision(batch_id: str, request_id: str, specimen_id: str, status: str) -> dict:
    return {
        'batch_id': batch_id,
        'specimen_id': specimen_id,
        'request_id': request_id,
        'status': status,
    }
