"""Envelopes, identities and bindings for prospective hand-off pieces.

Both piece types keep the V1 envelope (``batch_id``, ``roster_id``,
``specimens``, ``kind`` and ``source_id``) and the V1 rational encoding.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json

from bench.snapshot import validate_binary_result
from forecast.probability import fraction_payload, parse_probability
from study.identity import validate_identifier
from study.roster import Roster, roster_from_payload

PROSPECTIVE_FORECAST = 'prospective_forecast'
PROSPECTIVE_RESULTS = 'prospective_results'

FORECAST_FIELDS = {
    'kind', 'batch_id', 'roster_id', 'specimens', 'source_id',
    'specimen_id', 'probability', 'source', 'binding',
}
RESULTS_FIELDS = {'kind', 'batch_id', 'roster_id', 'specimens', 'source_id', 'rows'}
LOCK_FIELDS = {'request_id', 'binding'}


def prospective_closeout_id(batch_id: str) -> str:
    return f'prospective:{batch_id}'


def make_binding(
    batch_id: str, roster_id: str, specimen_id: str, request_id: str,
    probability: Fraction, source: str,
) -> str:
    """Opaque, stable digest binding a single pre-reveal forecast body."""
    material = json.dumps(
        [batch_id, roster_id, specimen_id, request_id,
         probability.numerator, probability.denominator, source],
        ensure_ascii=False, separators=(',', ':'),
    )
    return 'binding-' + sha256(material.encode('utf-8')).hexdigest()


def canonical_payload_text(payload: Mapping) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _validate_binding(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('binding must be a nonempty string')
    value.encode('utf-8')
    return value


@dataclass(frozen=True)
class ProspectiveForecast:
    roster: Roster
    request_id: str
    specimen_id: str
    probability: Fraction
    source: str
    binding: str

    @property
    def kind(self) -> str:
        return PROSPECTIVE_FORECAST

    @property
    def source_id(self) -> str:
        return self.request_id


@dataclass(frozen=True)
class ProspectiveLock:
    request_id: str
    binding: str


@dataclass(frozen=True)
class ProspectiveResultRow:
    specimen_id: str
    outcome: int
    source: str
    lock: ProspectiveLock | None


@dataclass(frozen=True)
class ProspectiveResults:
    roster: Roster
    closeout_id: str
    rows: tuple[ProspectiveResultRow, ...]

    @property
    def kind(self) -> str:
        return PROSPECTIVE_RESULTS

    @property
    def source_id(self) -> str:
        return self.closeout_id


def build_forecast_piece(
    roster: Roster, request_id: object, specimen_id: object,
    probability: object, source: object,
) -> ProspectiveForecast:
    request = validate_identifier(request_id, 'request_id')
    specimen = validate_identifier(specimen_id, 'specimen_id')
    if not roster.contains(specimen):
        raise ValueError(f'unknown prospective specimen {specimen!r}')
    value = parse_probability(probability)
    origin = validate_identifier(source, 'forecast source')
    return ProspectiveForecast(
        roster, request, specimen, value, origin,
        make_binding(roster.batch_id, roster.roster_id, specimen, request, value, origin),
    )


def forecast_piece_payload(piece: ProspectiveForecast) -> dict:
    return {
        'kind': PROSPECTIVE_FORECAST,
        **piece.roster.as_payload(),
        'source_id': piece.request_id,
        'specimen_id': piece.specimen_id,
        'probability': fraction_payload(piece.probability),
        'source': piece.source,
        'binding': piece.binding,
    }


def forecast_piece_from_payload(payload: Mapping) -> ProspectiveForecast:
    if not isinstance(payload, Mapping) or set(payload) != FORECAST_FIELDS:
        raise ValueError('invalid prospective forecast envelope')
    roster = roster_from_payload(payload)
    piece = build_forecast_piece(
        roster, payload['source_id'], payload['specimen_id'],
        payload['probability'], payload['source'],
    )
    if _validate_binding(payload['binding']) != piece.binding:
        raise ValueError('binding does not match forecast body and identity')
    return piece


def _build_lock(value: object) -> ProspectiveLock | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != LOCK_FIELDS:
        raise ValueError('lock must be null or carry request_id and binding')
    return ProspectiveLock(
        validate_identifier(value['request_id'], 'request_id'),
        _validate_binding(value['binding']),
    )


def build_results_piece(roster: Roster, closeout_id: object, rows: object) -> ProspectiveResults:
    closeout = validate_identifier(closeout_id, 'closeout_id')
    if not isinstance(rows, (list, tuple)):
        raise ValueError('result rows must be a list or tuple')
    expected = set(roster.specimens)
    by_specimen: dict[str, ProspectiveResultRow] = {}
    row_fields = {'specimen_id', 'outcome', 'source', 'lock'}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != row_fields:
            raise ValueError('result row requires specimen_id, outcome, source and lock')
        specimen = validate_identifier(row['specimen_id'], 'specimen_id')
        if specimen not in expected or specimen in by_specimen:
            raise ValueError(f'unknown or repeated result specimen {specimen!r}')
        by_specimen[specimen] = ProspectiveResultRow(
            specimen,
            validate_binary_result(row['outcome']),
            validate_identifier(row['source'], 'bench source'),
            _build_lock(row['lock']),
        )
    missing = [name for name in roster.specimens if name not in by_specimen]
    if missing:
        raise ValueError(f'missing revealed results: {missing!r}')
    return ProspectiveResults(roster, closeout,
                              tuple(by_specimen[name] for name in roster.specimens))


def results_piece_payload(piece: ProspectiveResults) -> dict:
    return {
        'kind': PROSPECTIVE_RESULTS,
        **piece.roster.as_payload(),
        'source_id': piece.closeout_id,
        'rows': [
            {
                'specimen_id': row.specimen_id,
                'outcome': row.outcome,
                'source': row.source,
                'lock': None if row.lock is None else {
                    'request_id': row.lock.request_id,
                    'binding': row.lock.binding,
                },
            }
            for row in piece.rows
        ],
    }


def results_piece_from_payload(payload: Mapping) -> ProspectiveResults:
    if not isinstance(payload, Mapping) or set(payload) != RESULTS_FIELDS:
        raise ValueError('invalid prospective results envelope')
    return build_results_piece(
        roster_from_payload(payload), payload.get('source_id'), payload.get('rows'),
    )
