from bench.snapshot import RevealedResult
from bench.store import BenchStore
from evaluation.accession import accept_piece
from prospective.pieces import ProspectiveForecast, forecast_piece_payload
from study.registry import require_specimen
from study.store import StudyStore


def lock_forecast(
    studies: StudyStore, bench: BenchStore, batch_id: str, specimen_id: str,
    request_id: str, probability: object, source: str, *, custodian=None,
) -> dict:
    roster = require_specimen(studies, batch_id, specimen_id)

    def custody(piece: ProspectiveForecast) -> None:
        # Kept on the evaluation side, outside the forecast source, so the exact
        # body survives even after the source closes.
        accept_piece(custodian, forecast_piece_payload(piece))

    return bench.lock_forecast(
        roster, request_id, specimen_id, probability, source,
        custody=None if custodian is None else custody,
    )


def read_locked_forecast(
    bench: BenchStore, batch_id: str, request_id: str, *, custodian=None,
) -> ProspectiveForecast:
    piece = bench.load_locked_forecast(batch_id, request_id)
    if custodian is not None:
        accept_piece(custodian, forecast_piece_payload(piece))
    return piece


def reveal_result(
    studies: StudyStore, bench: BenchStore, batch_id: str, specimen_id: str
) -> RevealedResult:
    roster = require_specimen(studies, batch_id, specimen_id)
    return bench.mark_revealed(roster.batch_id, specimen_id)
