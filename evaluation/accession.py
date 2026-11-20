from evaluation.piece_content import Piece, normalize_piece
from evaluation.store import AccessionStore


def accept_piece(store: AccessionStore, payload: object) -> str:
    return store.admit(normalize_piece(payload))


def get_accepted_piece(store: AccessionStore, accession_id: str) -> Piece:
    return store.load_piece(accession_id)
