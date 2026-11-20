from bench.snapshot import BenchSnapshot
from bench.store import BenchStore
from prospective.pieces import ProspectiveResults
from study.registry import get_roster
from study.store import StudyStore


def close_results(studies: StudyStore, bench: BenchStore, batch_id: str) -> BenchSnapshot:
    return bench.save_or_recall_closeout(get_roster(studies, batch_id))


def close_prospective(studies: StudyStore, bench: BenchStore, batch_id: str) -> ProspectiveResults:
    return bench.save_or_recall_prospective_closeout(get_roster(studies, batch_id))
