"""Replay two V1 histories with the same inputs to the evaluator."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3

from archive.lookup import read_review
from archive.store import ArchiveStore
from bench.closeout import close_results
from bench.results import record_result
from bench.reveal import reveal_result
from bench.snapshot import bench_piece_payload
from bench.store import BenchStore
from evaluation.accession import accept_piece
from evaluation.review import issue_review
from evaluation.store import AccessionStore
from forecast.revisions import freeze_forecast
from forecast.snapshot import forecast_piece_payload
from forecast.store import ForecastStore
from study.registry import create_study
from study.store import StudyStore


def replay(case, *, early):
    with ExitStack() as stack:
        def connect(store):
            conn = stack.enter_context(closing(sqlite3.connect(":memory:")))
            conn.execute("PRAGMA temp_store=MEMORY")
            return store(conn)

        studies, forecasts, bench, accessions, archive = map(
            connect, (StudyStore, ForecastStore, BenchStore, AccessionStore, ArchiveStore)
        )
        batch = case["batch_id"]
        create_study(studies, batch, case["specimens"])
        initial = case["forecasts"]
        amendment = case["amendment"]
        amended = [amendment if row["specimen_id"] == amendment["specimen_id"] else row for row in initial]

        def freeze(revision_key, rows):
            values = [{key: value for key, value in row.items() if key != "request_id"} for row in rows]
            snapshot = freeze_forecast(studies, forecasts, batch, case[revision_key], values)
            payload = forecast_piece_payload(snapshot)
            return payload, accept_piece(accessions, payload)

        for row in case["results"]:
            record_result(studies, bench, batch, **row)
        if early:
            first = freeze("initial_revision", initial)
        for specimen in case["specimens"]:
            reveal_result(studies, bench, batch, specimen)
        if not early:
            first = freeze("initial_revision", initial)
        second = freeze("late_revision", amended)
        results = bench_piece_payload(close_results(studies, bench, batch))
        results_id = accept_piece(accessions, results)
        forecasts.connection.close()
        reports = [issue_review(accessions, archive, token, results_id) for _, token in (first, second)]
        stored = [read_review(archive, report.report_id, as_json=True) for report in reports]
        return (first, second, results), stored, [str(report.body.mean) for report in reports]


def main():
    case = json.loads(Path(__file__).with_name("prospective_cases.json").read_text(encoding="utf-8"))
    early, late = replay(case, early=True), replay(case, early=False)
    if early != late or early[2] != case["expected"]["v1_means"]:
        raise RuntimeError("unexpected V1 archival result for the two reveal orders")
    print("V1_REVEAL_ORDER", json.dumps({"same_pieces": True, "same_reports": True, "means": early[2]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
