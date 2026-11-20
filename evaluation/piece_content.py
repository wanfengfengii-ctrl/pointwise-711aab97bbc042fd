from collections.abc import Mapping
from dataclasses import dataclass
import json

from bench.snapshot import BenchSnapshot, RevealedResult, bench_piece_payload, bench_snapshot_from_payload
from forecast.import_rows import ForecastRow
from forecast.snapshot import ForecastSnapshot, forecast_piece_payload, forecast_snapshot_from_payload
from prospective.pieces import (
    PROSPECTIVE_FORECAST, PROSPECTIVE_RESULTS, ProspectiveForecast, ProspectiveResults,
    forecast_piece_from_payload, forecast_piece_payload as prospective_forecast_payload,
    results_piece_from_payload, results_piece_payload as prospective_results_payload,
)
from study.roster import Roster


@dataclass(frozen=True)
class FixedPiece:
    kind: str
    roster: Roster
    source_id: str
    rows: tuple[ForecastRow, ...] | tuple[RevealedResult, ...]


Piece = FixedPiece | ProspectiveForecast | ProspectiveResults

_LEGACY_FIELDS = {'kind', 'batch_id', 'roster_id', 'specimens', 'source_id', 'rows'}


def normalize_piece(payload: object) -> Piece:
    if not isinstance(payload, Mapping):
        raise ValueError('invalid fixed-piece envelope')
    kind = payload.get('kind')
    if kind in ('forecast', 'results'):
        if set(payload) != _LEGACY_FIELDS:
            raise ValueError('invalid fixed-piece envelope')
        if kind == 'forecast':
            forecast = forecast_snapshot_from_payload(payload)
            return FixedPiece(kind, forecast.roster, forecast.revision_id, forecast.rows)
        results = bench_snapshot_from_payload(payload)
        return FixedPiece(kind, results.roster, results.closeout_id, results.rows)
    if kind == PROSPECTIVE_FORECAST:
        return forecast_piece_from_payload(payload)
    if kind == PROSPECTIVE_RESULTS:
        return results_piece_from_payload(payload)
    raise ValueError('unrecognized fixed-piece type')


def encode_piece(piece: Piece) -> str:
    if isinstance(piece, ProspectiveForecast):
        payload = prospective_forecast_payload(piece)
    elif isinstance(piece, ProspectiveResults):
        payload = prospective_results_payload(piece)
    elif piece.kind == 'forecast':
        payload = forecast_piece_payload(ForecastSnapshot(piece.roster, piece.source_id, piece.rows))
    elif piece.kind == 'results':
        payload = bench_piece_payload(BenchSnapshot(piece.roster, piece.source_id, piece.rows))
    else:
        raise ValueError('unrecognized fixed-piece type')
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode_piece(text: str) -> Piece:
    if not isinstance(text, str):
        raise ValueError('fixed-piece encoding must be JSON text')
    return normalize_piece(json.loads(text))
