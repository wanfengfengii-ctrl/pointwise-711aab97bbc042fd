"""Prospective review bodies, receipts and JSON rendering."""

from dataclasses import dataclass
from fractions import Fraction
import json

from forecast.probability import fraction_from_payload, fraction_payload
from study.identity import validate_identifier


@dataclass(frozen=True)
class ProspectiveReviewRow:
    specimen_id: str
    outcome: int
    result_source: str
    request_id: str | None
    forecast_accession: str | None
    forecast_source: str | None
    probability: Fraction | None
    squared_error: Fraction | None


@dataclass(frozen=True)
class ProspectiveReviewBody:
    batch_id: str
    roster_id: str
    results_accession: str
    results_closeout: str
    included: tuple[ProspectiveReviewRow, ...]
    excluded: tuple[ProspectiveReviewRow, ...]
    mean: Fraction | None


@dataclass(frozen=True)
class ProspectiveReviewReceipt:
    report_id: str


@dataclass(frozen=True)
class IssuedProspectiveReview:
    report_id: str
    body: ProspectiveReviewBody
    receipt: ProspectiveReviewReceipt


def _optional_fraction(value: Fraction | None):
    return None if value is None else fraction_payload(value)


def _row_payload(row: ProspectiveReviewRow) -> dict:
    payload = {
        'specimen_id': row.specimen_id,
        'outcome': row.outcome,
        'result_source': row.result_source,
    }
    if row.request_id is not None:
        payload['request_id'] = row.request_id
        payload['forecast_accession'] = row.forecast_accession
        payload['forecast_source'] = row.forecast_source
        payload['probability'] = _optional_fraction(row.probability)
        payload['squared_error'] = _optional_fraction(row.squared_error)
    return payload


def body_payload(body: ProspectiveReviewBody) -> dict:
    return {
        'batch_id': body.batch_id,
        'roster_id': body.roster_id,
        'results_accession': body.results_accession,
        'results_closeout': body.results_closeout,
        'included': [_row_payload(row) for row in body.included],
        'excluded': [_row_payload(row) for row in body.excluded],
        'mean': _optional_fraction(body.mean),
    }


def _json_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def encode_body(body: ProspectiveReviewBody) -> str:
    return _json_text(body_payload(body))


def _decode_row(row: dict, *, included: bool) -> ProspectiveReviewRow:
    specimen = validate_identifier(row['specimen_id'], 'specimen_id')
    if included:
        fields = {'specimen_id', 'outcome', 'result_source', 'request_id',
                  'forecast_accession', 'forecast_source', 'probability', 'squared_error'}
        if set(row) != fields:
            raise ValueError('included row has the wrong fields')
        return ProspectiveReviewRow(
            specimen, row['outcome'], row['result_source'], row['request_id'],
            row['forecast_accession'], row['forecast_source'],
            fraction_from_payload(row['probability']), fraction_from_payload(row['squared_error']),
        )
    if set(row) != {'specimen_id', 'outcome', 'result_source'}:
        raise ValueError('excluded row has the wrong fields')
    return ProspectiveReviewRow(specimen, row['outcome'], row['result_source'],
                                None, None, None, None, None)


def decode_body(text: str) -> ProspectiveReviewBody:
    payload = json.loads(text)
    mean = payload['mean']
    return ProspectiveReviewBody(
        payload['batch_id'], payload['roster_id'], payload['results_accession'],
        payload['results_closeout'],
        tuple(_decode_row(row, included=True) for row in payload['included']),
        tuple(_decode_row(row, included=False) for row in payload['excluded']),
        None if mean is None else fraction_from_payload(mean),
    )


def render_json(review: IssuedProspectiveReview) -> str:
    return _json_text({
        'report_id': review.report_id,
        'body': body_payload(review.body),
        'receipt': {'report_id': review.receipt.report_id},
    })
