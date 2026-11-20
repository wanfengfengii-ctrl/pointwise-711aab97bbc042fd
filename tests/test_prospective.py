import json
import sqlite3
from fractions import Fraction
from pathlib import Path

from archive.lookup import read_prospective_review
from bench.closeout import close_prospective
from bench.reveal import lock_forecast, read_locked_forecast, reveal_result
from bench.results import record_result
from bench.snapshot import RevealedResult
from evaluation.accession import accept_piece, get_accepted_piece
from evaluation.prospective_review import issue_prospective_review
from prospective.pieces import forecast_piece_payload, results_piece_payload
from study.registry import create_study
from tests.support import LabCase

BATCH_ID = 'at-17'
SPECIMENS = ('C', 'B', 'A')
RESULTS = {'A': 1, 'B': 0, 'C': 1}


class ProspectiveCase(LabCase):
    """Shared setup with C, B, A roster, private results and lock helpers."""

    def setUp(self):
        super().setUp()
        self.studies = self.reopen(self.studies, 'study')
        self.register(batch_id=BATCH_ID, specimens=SPECIMENS)
        for name in SPECIMENS:
            record_result(self.studies, self.bench, BATCH_ID, name, RESULTS[name], f'bench:{name}')

    def _archive_counts(self):
        return self.archive.connection.execute(
            'SELECT (SELECT count(*) FROM prospective_reports), '
            '(SELECT count(*) FROM prospective_receipts)').fetchone()

    def lock(self, specimen, request_id, probability, source, *, custodian=True):
        return lock_forecast(
            self.studies, self.bench, BATCH_ID, specimen, request_id, probability, source,
            custodian=self.accessions if custodian else None,
        )

    def reveal(self, *names):
        for name in names:
            reveal_result(self.studies, self.bench, BATCH_ID, name)

    def close(self):
        return close_prospective(self.studies, self.bench, BATCH_ID)

    def accept(self, piece):
        return accept_piece(self.accessions, piece)

    def pending(self, results_id, forecast_ids):
        return issue_prospective_review(self.accessions, self.archive, results_id, forecast_ids)

    def standard_locks(self):
        self.lock('A', 'lock-A', '1/4', 'r1:A')
        self.lock('C', 'lock-C', '1', 'r1:C')

    def results_id(self):
        return self.accept(results_piece_payload(self.close()))


class LockTests(ProspectiveCase):
    def test_lock_statuses_and_replay(self):
        self.assertEqual(self.lock('A', 'lock-A', '1/4', 'r1:A'),
                         {'batch_id': BATCH_ID, 'specimen_id': 'A',
                          'request_id': 'lock-A', 'status': 'locked'})
        self.assertEqual(self.lock('A', 'lock-A2', '1/2', 'r1:A2')['status'], 'already_locked')
        self.reveal('A')
        self.assertEqual(self.lock('A', 'late-A', '3/4', 'r2:A')['status'], 'revealed')
        self.reveal('B')
        self.assertEqual(self.lock('B', 'late-B', 0, 'r1:B')['status'], 'revealed')
        self.assertEqual(self.lock('A', 'lock-A', '1/4', 'r1:A')['status'], 'locked')
        self.assertEqual(self.lock('A', 'late-A', '3/4', 'r2:A')['status'], 'revealed')
        self.assertEqual(self.lock('A', 'lock-A2', '1/2', 'r1:A2')['status'], 'already_locked')

    def test_request_identity_conflicts(self):
        self.lock('A', 'lock-A', '1/4', 'r1:A')
        for specimen, probability, source in (
            ('C', '1/4', 'r1:A'), ('A', '1/2', 'r1:A'), ('A', '1/4', 'other:A'),
        ):
            with self.subTest(specimen=specimen, probability=probability, source=source):
                with self.assertRaises(ValueError):
                    self.lock(specimen, 'lock-A', probability, source)
        self.assertEqual(self.lock('A', 'lock-A', '2/8', 'r1:A')['status'], 'locked')

    def test_single_lock_per_specimen(self):
        self.assertEqual(self.lock('A', 'one', '1/4', 'r1:A')['status'], 'locked')
        self.assertEqual(self.lock('A', 'two', '1/4', 'r1:A')['status'], 'already_locked')
        self.reveal('A')
        self.assertEqual(self.lock('A', 'three', '1/4', 'r1:A')['status'], 'revealed')

    def test_read_locked_survives_source_close(self):
        self.lock('A', 'lock-A', '1/4', 'r1:A')
        self.forecasts.connection.close()
        piece = read_locked_forecast(self.bench, BATCH_ID, 'lock-A')
        self.assertEqual(piece.specimen_id, 'A')
        self.assertEqual(piece.probability, Fraction(1, 4))
        self.assertEqual(piece.source, 'r1:A')
        self.assertEqual(piece.request_id, 'lock-A')
        self.assertEqual(read_locked_forecast(self.bench, BATCH_ID, 'lock-A'), piece)
        with self.assertRaises(KeyError):
            read_locked_forecast(self.bench, BATCH_ID, 'unknown')
        self.lock('A', 'refused', '1/2', 'r1:A')
        with self.assertRaises(ValueError):
            read_locked_forecast(self.bench, BATCH_ID, 'refused')
        self.reveal('A')
        self.lock('A', 'late', '3/4', 'r2:A')
        with self.assertRaises(ValueError):
            read_locked_forecast(self.bench, BATCH_ID, 'late')

    def test_reveal_does_not_depend_on_custody(self):
        self.lock('A', 'lock-A', '1/4', 'r1:A')
        self.accessions.connection.close()
        self.reveal('A')
        self.assertEqual(reveal_result(self.studies, self.bench, BATCH_ID, 'A'),
                         RevealedResult('A', 1, 'bench:A'))

    def test_custody_failure_prevents_lock(self):
        class BrokenCustodian:
            def admit(self, piece):
                raise ValueError('custody refused')

        with self.assertRaises(ValueError):
            lock_forecast(self.studies, self.bench, BATCH_ID, 'A', 'lock-A', '1/4', 'r1:A',
                          custodian=BrokenCustodian())
        self.assertEqual(
            self.bench.connection.execute('SELECT count(*) FROM bench_locks').fetchone(), (0,))
        self.assertEqual(self.lock('A', 'lock-A', '1/4', 'r1:A')['status'], 'locked')

    def test_decision_follows_bench_order(self):
        # Same trial-side inputs, opposite local commit order: a lock submitted
        # before this bench records reveal is locked; one submitted after is
        # revealed. No cross-database clock is consulted.
        self.reveal('A')
        self.assertEqual(self.lock('A', 'after', '1/4', 'r1:A')['status'], 'revealed')
        # B stays private, so its lock is accepted even though A was revealed.
        self.assertEqual(self.lock('B', 'before', 0, 'r1:B')['status'], 'locked')
        self.bench = self.reopen(self.bench, 'bench')
        self.assertEqual(self.lock('A', 'after', '1/4', 'r1:A')['status'], 'revealed')
        self.assertEqual(self.lock('B', 'before', 0, 'r1:B')['status'], 'locked')
        self.assertEqual(self.lock('B', 'second', 0, 'r1:B')['status'], 'already_locked')


class CloseoutTests(ProspectiveCase):
    def test_requires_full_roster(self):
        self.reveal('A', 'C')
        with self.assertRaisesRegex(ValueError, 'B'):
            self.close()
        self.assertEqual(
            self.bench.connection.execute('SELECT count(*) FROM prospective_closeouts').fetchone(), (0,))
        self.reveal('B')
        snapshot = self.close()
        self.assertEqual(tuple(row.specimen_id for row in snapshot.rows), SPECIMENS)
        self.assertEqual(snapshot.closeout_id, f'prospective:{BATCH_ID}')
        self.assertEqual(self.close(), snapshot)

    def test_lock_qualification_is_frozen(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        snapshot = self.close()
        locks = {row.specimen_id: row.lock for row in snapshot.rows}
        self.assertIsNone(locks['B'])
        self.assertEqual(locks['A'].request_id, 'lock-A')
        self.assertEqual(locks['C'].request_id, 'lock-C')
        # A submitted after full reveal is refused and cannot change the sealed row.
        self.assertEqual(self.lock('B', 'late-B', 0, 'r1:B')['status'], 'revealed')
        self.assertEqual(self.close(), snapshot)
        payload = results_piece_payload(snapshot)
        self.assertEqual(payload['kind'], 'prospective_results')
        self.assertEqual(payload['source_id'], f'prospective:{BATCH_ID}')
        for row in payload['rows']:
            self.assertEqual(set(row), {'specimen_id', 'outcome', 'source', 'lock'})
        self.assertIsNone(payload['rows'][1]['lock'])


class AccessionTests(ProspectiveCase):
    def test_acceptance_and_conflict(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results = results_piece_payload(self.close())
        results_id = self.accept(results)
        forecast_id = self.accept(forecast_piece_payload(
            read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        self.assertEqual(self.accept(results), results_id)
        altered = json.loads(json.dumps(results))
        altered['rows'][0]['outcome'] = 0
        with self.assertRaises(ValueError):
            self.accept(altered)
        piece = get_accepted_piece(self.accessions, forecast_id)
        self.assertEqual(piece.probability, Fraction(1, 4))

    def test_reject_broken_bindings(self):
        self.standard_locks()
        piece = forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A'))
        for mutate in (
            lambda p: p.update(binding='binding-forged'),
            lambda p: (p.update(binding=''), None),
            lambda p: p.update(probability={'numerator': 3, 'denominator': 4}),
            lambda p: p.update(specimen_id='B'),
            lambda p: p.update(source_id='other-request'),
            lambda p: p.update(roster_id='invented'),
        ):
            bad = json.loads(json.dumps(piece))
            mutate(bad)
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    accept_piece(self.accessions, bad)


class ReviewTests(ProspectiveCase):
    def test_delivery_order_is_free(self):
        # Either piece may arrive first; accession survives across calls and
        # before any lock on the evaluation side.
        self.lock('A', 'lock-A', '1/4', 'r1:A', custodian=False)
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        piece = read_locked_forecast(self.bench, BATCH_ID, 'lock-A')
        forecast_id = self.accept(forecast_piece_payload(piece))
        self.accessions = self.reopen(self.accessions, 'accession')
        report = self.pending(results_id, [forecast_id])
        self.assertEqual(report['status'], 'issued')
        self.assertEqual(report['body']['included'][0]['probability'],
                         {'numerator': 1, 'denominator': 4})

    def test_late_amendment_after_issue_cannot_alter(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        report = self.pending(results_id, [c_id, a_id])
        signed = read_prospective_review(self.archive, report['report_id'])
        late = self.lock('A', 'late-A', '3/4', 'r2:A')
        self.assertEqual(late['status'], 'revealed')
        # Re-seal and any further acceptance leave the signed body intact.
        self.accept(results_piece_payload(self.close()))
        self.assertEqual(read_prospective_review(self.archive, report['report_id']), signed)
        self.assertEqual(signed['body']['mean'], {'numerator': 9, 'denominator': 32})

    def test_specimen_identity_cannot_be_swapped(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        a_piece = forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A'))
        swapped = json.loads(json.dumps(a_piece))
        swapped['specimen_id'] = 'C'
        with self.assertRaises(ValueError):
            accept_piece(self.accessions, swapped)
        # A self-consistent forged body for another specimen is accepted as a
        # piece but cannot satisfy the sealed lock at review time.
        from prospective.pieces import make_binding
        forged_c = json.loads(json.dumps(a_piece))
        forged_c['specimen_id'] = 'C'
        forged_c['source_id'] = 'forged-request'
        forged_c['binding'] = make_binding(
            forged_c['batch_id'], forged_c['roster_id'], 'C', 'forged-request',
            Fraction(1, 4), 'r1:A')
        forged_id = self.accept(forged_c)
        with self.assertRaises(ValueError):
            self.pending(results_id, [forged_id])

    def test_source_closed_after_custody(self):
        # Disconnect the trial and forecast databases first: everything the
        # evaluation needs must already sit in the custodian and the archive.
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        self.forecasts.connection.close()
        self.studies.connection.close()
        self.bench.connection.close()
        stored = {get_accepted_piece(self.accessions, token).specimen_id: token
                  for token in ('piece-1', 'piece-2')}
        report = self.pending(results_id, [stored['C'], stored['A']])
        self.assertEqual(report['status'], 'issued')
        self.assertEqual(report['body']['mean'], {'numerator': 9, 'denominator': 32})
        self.assertEqual(read_prospective_review(self.archive, report['report_id']), report)

    def test_pending_then_issued(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        self.assertEqual(self.pending(results_id, [c_id]), {'status': 'pending', 'missing': ['A']})
        self.assertEqual(self.pending(results_id, []), {'status': 'pending', 'missing': ['C', 'A']})
        self.assertEqual(self.pending(results_id, (a_id,)), self.pending(results_id, [a_id]))
        self.assertEqual(self._archive_counts(), (0, 0))
        report = self.pending(results_id, [c_id, a_id])
        self.assertEqual(report['receipt'], {'report_id': report['report_id']})
        body = report['body']
        self.assertEqual(tuple(row['specimen_id'] for row in body['included']), ('C', 'A'))
        self.assertEqual(tuple(row['specimen_id'] for row in body['excluded']), ('B',))
        self.assertEqual(body['included'][1]['squared_error'], {'numerator': 9, 'denominator': 16})
        self.assertEqual(body['mean'], {'numerator': 9, 'denominator': 32})
        self.assertEqual(body['results_closeout'], f'prospective:{BATCH_ID}')
        recalled = read_prospective_review(self.archive, report['report_id'])
        self.assertEqual(recalled, report)
        self.assertEqual(read_prospective_review(self.archive, report['report_id'], as_json=True),
                         json.dumps(report, sort_keys=True, separators=(',', ':')))

    def test_unlocked_only_has_no_mean(self):
        self.reveal('C', 'B', 'A')
        report = self.pending(self.results_id(), None)
        self.assertEqual(report['status'], 'issued')
        self.assertEqual(report['body']['included'], [])
        self.assertIsNone(report['body']['mean'])
        self.assertEqual(tuple(row['specimen_id'] for row in report['body']['excluded']), SPECIMENS)

    def test_zero_error(self):
        self.lock('A', 'lock-A', 1, 'r1:A')
        self.reveal('C', 'B', 'A')
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        report = self.pending(self.results_id(), [a_id])
        self.assertEqual(report['body']['included'][0]['squared_error'], {'numerator': 0, 'denominator': 1})
        self.assertEqual(report['body']['mean'], {'numerator': 0, 'denominator': 1})
        self.assertEqual(read_prospective_review(self.archive, report['report_id']), report)

    def test_late_amendment_cannot_replace_orig(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        self.lock('A', 'late-A', '3/4', 'r2:A')
        results_id = self.results_id()
        # The amendment was refused, so it has no body to deliver.
        with self.assertRaises(ValueError):
            read_locked_forecast(self.bench, BATCH_ID, 'late-A')
        lock_a = read_locked_forecast(self.bench, BATCH_ID, 'lock-A')
        forged = forecast_piece_payload(lock_a)
        forged['probability'] = {'numerator': 3, 'denominator': 4}
        with self.assertRaises(ValueError):
            self.accept(forged)
        old_a = self.accept(forecast_piece_payload(lock_a))
        old_c = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        report = self.pending(results_id, [old_c, old_a])
        self.assertEqual(report['body']['included'][1]['probability'], {'numerator': 1, 'denominator': 4})
        self.assertEqual(report['body']['mean'], {'numerator': 9, 'denominator': 32})

    def test_identity_swap_and_mismatch(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        a_piece = forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A'))
        a_id = self.accept(a_piece)
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        swapped = json.loads(json.dumps(a_piece))
        swapped['specimen_id'] = 'C'
        with self.assertRaises(ValueError):
            accept_piece(self.accessions, swapped)
        # A body delivered for a locked specimen it is not the lock for is a mismatch.
        with self.assertRaises(ValueError):
            self.pending(results_id, [a_id, c_id, a_id])
        with self.assertRaises(KeyError):
            self.pending(results_id, ['piece-999'])
        with self.assertRaises(ValueError):
            self.pending(results_id, [a_id, a_id])
        report = self.pending(results_id, [a_id, c_id])
        self.assertEqual(report['status'], 'issued')

    def test_recomputed_binding_still_mismatch(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        from prospective.pieces import make_binding
        original = read_locked_forecast(self.bench, BATCH_ID, 'lock-A')
        forged = forecast_piece_payload(original)
        forged['probability'] = {'numerator': 3, 'denominator': 4}
        forged['binding'] = make_binding(
            forged['batch_id'], forged['roster_id'], 'A', 'alt-A', Fraction(3, 4), 'r1:A',
        )
        forged['source_id'] = 'alt-A'
        forged_id = self.accept(forged)  # internally consistent, but not the sealed lock
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        with self.assertRaises(ValueError):
            self.pending(results_id, [c_id, forged_id])

    def test_request_reuse_across_batches(self):
        self.standard_locks()
        other = create_study(self.studies, 'other-at', ('A',))
        from bench.results import record_result
        record_result(self.studies, self.bench, 'other-at', 'A', 1, 'bench:other:A')
        first = lock_forecast(self.studies, self.bench, 'other-at', 'A', 'lock-A', '1/4', 'r1:A')
        self.assertEqual(first['status'], 'locked')
        self.assertEqual(self.lock('A', 'lock-A', '1/4', 'r1:A')['status'], 'locked')

    def test_unlocked_delivery_rejected(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        sealed = results_piece_payload(self.close())
        a_piece = forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A'))
        c_piece = forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C'))
        forged = json.loads(json.dumps(sealed))
        for row in forged['rows']:
            if row['specimen_id'] == 'A':
                row['lock'] = None
        self.accept(forged)  # internally consistent, but treats A as unlocked
        a_id = self.accept(a_piece)
        c_id = self.accept(c_piece)
        with self.assertRaises(ValueError):
            self.pending('piece-1', [c_id, a_id])

    def test_signed_value_is_immutable(self):
        report = None
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        report = self.pending(results_id, [c_id, a_id])
        stored = read_prospective_review(self.archive, report['report_id'])
        # New acceptances, a re-seal and a re-open do not disturb the signed value.
        self.accept(results_piece_payload(self.close()))
        self.assertEqual(self.close().rows[0].lock.request_id, 'lock-C')
        self.accessions.connection.close()
        self.archive = self.reopen(self.archive, 'archive')
        self.assertEqual(read_prospective_review(self.archive, report['report_id']), stored)
        self.assertEqual(read_prospective_review(self.archive, report['report_id'], as_json=True),
                         json.dumps(stored, sort_keys=True, separators=(',', ':')))
        with self.assertRaises(KeyError):
            read_prospective_review(self.archive, 'prospective-review-999')


class CommitFailureTests(ProspectiveCase):
    def test_issue_is_atomic_and_retryable(self):
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        results_id = self.results_id()
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        connection = self.archive.connection
        connection.executescript('''
            CREATE TEMP TRIGGER refuse_prospective_receipt
            BEFORE INSERT ON main.prospective_receipts
            BEGIN
                SELECT RAISE(ABORT, 'receipt write refused');
            END;
        ''')
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'receipt write refused'):
                self.pending(results_id, [c_id, a_id])
            self.assertFalse(connection.in_transaction)
            counts = connection.execute(
                'SELECT (SELECT count(*) FROM prospective_reports), '
                '(SELECT count(*) FROM prospective_receipts)').fetchone()
            self.assertEqual(counts, (0, 0))
        finally:
            connection.execute('DROP TRIGGER temp.refuse_prospective_receipt')
        report = self.pending(results_id, [c_id, a_id])
        self.assertEqual(report['status'], 'issued')
        self.assertEqual(read_prospective_review(self.archive, report['report_id']), report)


class ScenarioEvidenceTests(ProspectiveCase):
    """Mirror examples/prospective_cases.json expectations."""

    def test_case_file_controls(self):
        case_path = Path(__file__).resolve().parent.parent / 'examples' / 'prospective_cases.json'
        expected = json.loads(case_path.read_text(encoding='utf-8'))['expected']
        self.standard_locks()
        self.reveal('C', 'B', 'A')
        self.assertEqual(self.lock('B', 'late-B', 0, 'r1:B')['status'], 'revealed')
        results_id = self.results_id()
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        c_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-C')))
        pending = self.pending(results_id, [c_id])
        self.assertEqual(pending, {'status': 'pending', 'missing': expected['pending']})
        report = self.pending(results_id, [c_id, a_id])
        body = report['body']
        self.assertEqual([row['specimen_id'] for row in body['included']], expected['included'])
        self.assertEqual([row['specimen_id'] for row in body['excluded']], expected['excluded'])
        self.assertEqual(body['included'][1]['squared_error'], {'numerator': 9, 'denominator': 16})
        self.assertEqual(body['mean'], {'numerator': 9, 'denominator': 32})
        print('PROSPECTIVE_SCENARIO ' + json.dumps({
            'pending': expected['pending'],
            'included': expected['included'],
            'excluded': expected['excluded'],
            'a_error': expected['a_error'],
            'mean': expected['mean'],
            'report_id': report['report_id'],
        }, sort_keys=True, separators=(',', ':')))

    def test_only_a_locked_gives_nine_sixteenths(self):
        self.lock('A', 'lock-A', '1/4', 'r1:A')
        self.reveal('C', 'B', 'A')
        a_id = self.accept(forecast_piece_payload(read_locked_forecast(self.bench, BATCH_ID, 'lock-A')))
        report = self.pending(self.results_id(), [a_id])
        self.assertEqual(report['body']['included'][0]['squared_error'],
                         {'numerator': 9, 'denominator': 16})
        self.assertEqual(report['body']['mean'], {'numerator': 9, 'denominator': 16})
        self.assertEqual([row['specimen_id'] for row in report['body']['excluded']], ['C', 'B'])
