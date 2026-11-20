"""Pre-unblinding prospective scoring: locks, hand-off and review."""

from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from archive.lookup import read_prospective_review
from archive.store import ArchiveStore
from bench.closeout import close_prospective
from bench.results import record_result
from bench.reveal import lock_forecast, read_locked_forecast, reveal_result
from bench.store import BenchStore
from evaluation.accession import accept_piece
from evaluation.prospective_review import issue_prospective_review
from evaluation.store import AccessionStore
from forecast.store import ForecastStore
from prospective.pieces import forecast_piece_payload, results_piece_payload
from study.registry import create_study
from study.store import StudyStore


BATCH = 'liwan-prospective-17'
SPECIMENS = ('C', 'B', 'A')
RESULTS = {'A': 1, 'B': 0, 'C': 1}


class LostReply(ConnectionError):
    """The request was committed but its reply was lost in transit."""


class Courier:
    """Hold encoded pieces until explicitly delivered to a real accession store."""

    def __init__(self):
        self._held: dict[str, dict] = {}

    def hold(self, delivery_id: str, payload: dict) -> None:
        if delivery_id in self._held:
            raise ValueError('delivery identifier already used')
        self._held[delivery_id] = json.loads(json.dumps(payload))

    def deliver(self, delivery_id: str, store: AccessionStore, *, lose_reply: bool = False) -> str:
        accession_id = accept_piece(store, self._held.pop(delivery_id))
        if lose_reply:
            raise LostReply(delivery_id)
        return accession_id


def _submit_lock(*args, lose_reply: bool = False, **kwargs) -> dict:
    decision = lock_forecast(*args, **kwargs)
    if lose_reply:
        raise LostReply(args[4])  # request_id committed; reply lost in transit
    return decision

def run_prospective_workday() -> dict:
    with TemporaryDirectory(prefix='liwan-prospective-') as directory, ExitStack() as stack:
        def connect(name: str) -> sqlite3.Connection:
            return stack.enter_context(closing(sqlite3.connect(Path(directory) / f'{name}.sqlite3')))

        studies = StudyStore(connect('study'))
        forecasts = ForecastStore(connect('forecast'))
        bench = BenchStore(connect('bench'))
        accessions = AccessionStore(connect('accession'))
        archive = ArchiveStore(connect('archive'))
        courier = Courier()

        create_study(studies, BATCH, SPECIMENS)
        for name in SPECIMENS:
            record_result(studies, bench, BATCH, name, RESULTS[name], f'bench:{name}')

        # Lock A, then C. A's reply is lost after the trial-end decision is
        # committed; the retry re-claims the original decision.
        try:
            _submit_lock(
                studies, bench, BATCH, 'A', 'lock-A', '1/4', 'r1:A',
                custodian=accessions, lose_reply=True,
            )
        except LostReply:
            pass
        lock_a = _submit_lock(studies, bench, BATCH, 'A', 'lock-A', '1/4', 'r1:A', custodian=accessions)
        lock_c = _submit_lock(studies, bench, BATCH, 'C', 'lock-C', 1, 'r1:C', custodian=accessions)

        # Actual unblinding always uses the original entry point.
        reveal_result(studies, bench, BATCH, 'A')
        reveal_result(studies, bench, BATCH, 'B')
        reveal_result(studies, bench, BATCH, 'C')

        # B is requested only after its reveal.
        lock_b = _submit_lock(studies, bench, BATCH, 'B', 'late-B', 0, 'r1:B')

        # The forecast source closes; the original locked body is still
        # available from the trial side and from evaluation custody.
        forecasts.connection.close()
        locked_a = read_locked_forecast(bench, BATCH, 'lock-A', custodian=accessions)

        closeout = close_prospective(studies, bench, BATCH)
        results_id = accept_piece(accessions, results_piece_payload(closeout))
        courier.hold('C-body', forecast_piece_payload(read_locked_forecast(bench, BATCH, 'lock-C')))
        courier.hold('A-body', forecast_piece_payload(locked_a))
        c_id = courier.deliver('C-body', accessions)
        pending = issue_prospective_review(accessions, archive, results_id, [c_id])
        try:
            courier.deliver('A-body', accessions, lose_reply=True)
        except LostReply:
            pass
        # The lost accession reply is re-obtained by re-delivering the same body.
        courier.hold('A-retry', forecast_piece_payload(locked_a))
        a_id = courier.deliver('A-retry', accessions)
        report = issue_prospective_review(accessions, archive, results_id, [c_id, a_id])

        # A late amendment with the same specimen cannot replace the original.
        late = _submit_lock(studies, bench, BATCH, 'A', 'late-A', '3/4', 'r2:A')
        try:
            read_locked_forecast(bench, BATCH, 'late-A')
            amendment_error = None
        except ValueError as exc:
            amendment_error = str(exc)

        archive.connection.close()
        reopened = ArchiveStore(connect('archive'))
        recalled = read_prospective_review(reopened, report['report_id'])
        controls = _run_controls(connect)
        return {
            'locks': {'C': lock_c, 'A': lock_a, 'B': lock_b, 'late_A': late},
            'locked_a': locked_a,
            'pending': pending,
            'report': report,
            'recalled': recalled,
            'amendment_error': amendment_error,
            'controls': controls,
        }


def _run_controls(connect) -> dict:
    """Same roster, two contrasting lock sets, on fresh local databases."""
    controls = {}
    for name, locks in (
        ('none', []),
        ('only-A', [('A', 'lock-A', '1/4', 'r1:A')]),
        ('only-C', [('C', 'lock-C', '1', 'r1:C')]),
    ):
        studies = StudyStore(connect(f'{name}-study'))
        bench = BenchStore(connect(f'{name}-bench'))
        accessions = AccessionStore(connect(f'{name}-accession'))
        archive = ArchiveStore(connect(f'{name}-archive'))
        create_study(studies, BATCH, SPECIMENS)
        for specimen in SPECIMENS:
            record_result(studies, bench, BATCH, specimen, RESULTS[specimen], f'bench:{specimen}')
        for specimen, request, probability, source in locks:
            lock_forecast(studies, bench, BATCH, specimen, request, probability, source,
                          custodian=accessions)
        for specimen in SPECIMENS:
            reveal_result(studies, bench, BATCH, specimen)
        results_id = accept_piece(
            accessions, results_piece_payload(close_prospective(studies, bench, BATCH)))
        report = issue_prospective_review(
            accessions, archive, results_id, ['piece-1'] if locks else [])
        controls[name] = {
            'included': [row['specimen_id'] for row in report['body']['included']],
            'mean': report['body']['mean'],
        }
    return controls


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='Run a prospective scoring workday.')
    parser.parse_args(argv)
    try:
        day = run_prospective_workday()
    except (ValueError, KeyError, sqlite3.Error, OSError, AssertionError) as exc:
        parser.exit(1, f'prospective workday failed: {exc}\n')
    report = day['report']
    summary = {
        'locks': day['locks'],
        'pending_missing': day['pending']['missing'],
        'locked_a': {
            'specimen_id': day['locked_a'].specimen_id,
            'request_id': day['locked_a'].request_id,
            'probability': str(day['locked_a'].probability),
            'source': day['locked_a'].source,
        },
        'included': [row['specimen_id'] for row in report['body']['included']],
        'excluded': [row['specimen_id'] for row in report['body']['excluded']],
        'a_error': report['body']['included'][1]['squared_error'],
        'mean': report['body']['mean'],
        'report_id': report['report_id'],
        'recalled_equal': day['recalled'] == report,
        'amendment_error': day['amendment_error'],
        'controls': day['controls'],
    }
    print('PROSPECTIVE_DAY ' + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
