import json

from archive.export import IssuedReview, ReviewReceipt, decode_review_body, render_review_json
from archive.prospective_export import body_payload, decode_body
from archive.store import ArchiveStore


def read_review(store: ArchiveStore, report_id: str, *, as_json: bool = False) -> IssuedReview | str:
    body, receipt_id = store.load_review(report_id)
    review = IssuedReview(report_id, decode_review_body(body), ReviewReceipt(receipt_id))
    return render_review_json(review) if as_json else review


def read_prospective_review(store: ArchiveStore, report_id: str, *, as_json: bool = False) -> dict | str:
    """Recall the exact signed prospective report by its report number."""
    body, receipt_id = store.load_prospective(report_id)
    report = {
        'status': 'issued',
        'report_id': report_id,
        'receipt': {'report_id': receipt_id},
        'body': body_payload(decode_body(body)),
    }
    if as_json:
        return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return report
