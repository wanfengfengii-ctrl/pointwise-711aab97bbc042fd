"""Issue a prospective review report after verifying all delivered evidence."""

from dataclasses import dataclass
from fractions import Fraction

from archive.prospective_export import (
    ProspectiveReviewBody, ProspectiveReviewRow, body_payload, encode_body,
)
from archive.store import ArchiveStore
from evaluation.store import AccessionStore
from prospective.pieces import (
    PROSPECTIVE_FORECAST, PROSPECTIVE_RESULTS, ProspectiveForecast, ProspectiveResults,
)
from study.identity import validate_identifier


@dataclass(frozen=True)
class _ForecastEvidence:
    accession_id: str
    piece: ProspectiveForecast


def _selected_tokens(forecast_accessions: object) -> tuple[str, ...]:
    if forecast_accessions is None:
        return ()
    if not isinstance(forecast_accessions, (list, tuple)):
        raise ValueError('forecast_accessions must be a list, tuple or None')
    tokens = tuple(forecast_accessions)
    for token in tokens:
        validate_identifier(token, 'forecast_accession')
    if len(set(tokens)) != len(tokens):
        raise ValueError('forecast accession list has duplicates')
    return tokens


def _delivered_forecasts(
    accessions: AccessionStore, tokens: tuple[str, ...], roster,
) -> dict[str, _ForecastEvidence]:
    delivered: dict[str, _ForecastEvidence] = {}
    for token in tokens:
        piece = accessions.load_piece(token)
        if not isinstance(piece, ProspectiveForecast) or piece.kind != PROSPECTIVE_FORECAST:
            raise ValueError('forecast selection contains a non-forecast piece')
        if piece.roster != roster:
            raise ValueError('forecast piece has a different batch or ordered roster')
        if piece.specimen_id in delivered:
            raise ValueError(f'multiple delivered forecast bodies for {piece.specimen_id!r}')
        delivered[piece.specimen_id] = _ForecastEvidence(token, piece)
    return delivered


def issue_prospective_review(
    accessions: AccessionStore, archive: ArchiveStore,
    results_accession: str, forecast_accessions: object,
) -> dict:
    results = accessions.load_piece(results_accession)
    if not isinstance(results, ProspectiveResults) or results.kind != PROSPECTIVE_RESULTS:
        raise ValueError('results selection must be a prospective results piece')
    roster = results.roster

    delivered = _delivered_forecasts(accessions, _selected_tokens(forecast_accessions), roster)

    ledger = {row.specimen_id: row for row in results.rows}
    for specimen, evidence in delivered.items():
        row = ledger[specimen]
        if row.lock is None:
            raise ValueError(f'delivered forecast for unlocked specimen {specimen!r}')
        piece = evidence.piece
        if piece.request_id != row.lock.request_id or piece.binding != row.lock.binding:
            raise ValueError(
                f'delivered forecast for {specimen!r} does not match the recorded lock'
            )

    missing = [row.specimen_id for row in results.rows
               if row.lock is not None and row.specimen_id not in delivered]
    if missing:
        return {'status': 'pending', 'missing': missing}

    included: list[ProspectiveReviewRow] = []
    excluded: list[ProspectiveReviewRow] = []
    total = Fraction(0)
    for row in results.rows:
        if row.lock is None:
            excluded.append(ProspectiveReviewRow(
                row.specimen_id, row.outcome, row.source, None, None, None, None, None,
            ))
            continue
        evidence = delivered[row.specimen_id]
        piece = evidence.piece
        error = (piece.probability - row.outcome) ** 2
        total += error
        included.append(ProspectiveReviewRow(
            row.specimen_id, row.outcome, row.source, piece.request_id,
            evidence.accession_id, piece.source, piece.probability, error,
        ))
    mean = None if not included else total / len(included)

    body = ProspectiveReviewBody(
        roster.batch_id, roster.roster_id, results_accession, results.closeout_id,
        tuple(included), tuple(excluded), mean,
    )
    report_id = archive.commit_prospective(encode_body(body))
    return {
        'status': 'issued',
        'report_id': report_id,
        'receipt': {'report_id': report_id},
        'body': body_payload(body),
    }
