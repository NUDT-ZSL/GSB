from coord_workbench.errors import BizError, Err
from coord_workbench.models import (
    OUTCOME_ABANDONED,
    OUTCOME_COMMITTED,
    OUTCOME_NOT_STARTED,
    OUTCOME_START_FAILED,
    Batch,
)
from coord_workbench.scheduler import MergeScheduler


def _batch(i, cost=10):
    return Batch(batch_id=f"b{i}", domain="d", topo_epoch=1, est_cost_ms=cost)


def test_all_committed_within_limits() -> None:
    recs = MergeScheduler(max_concurrency=3, time_budget_ms=100).run(
        [_batch(i) for i in range(3)], lambda b: b.est_cost_ms
    )
    assert [r["outcome"] for r in recs] == [OUTCOME_COMMITTED] * 3


def test_concurrency_limit() -> None:
    recs = MergeScheduler(max_concurrency=2, time_budget_ms=100).run(
        [_batch(i) for i in range(4)], lambda b: b.est_cost_ms
    )
    assert [r["outcome"] for r in recs] == [
        OUTCOME_COMMITTED, OUTCOME_COMMITTED, OUTCOME_NOT_STARTED, OUTCOME_NOT_STARTED]
    assert recs[2]["reason"] == "concurrency-limit"


def test_budget_exhausted() -> None:
    recs = MergeScheduler(max_concurrency=10, time_budget_ms=25).run(
        [_batch(i) for i in range(3)], lambda b: b.est_cost_ms
    )
    assert [r["outcome"] for r in recs] == [OUTCOME_COMMITTED, OUTCOME_COMMITTED, OUTCOME_NOT_STARTED]
    assert recs[2]["reason"] == "budget-exhausted"


def test_started_batch_never_truncated_and_rest_abandoned() -> None:
    # actual cost overruns the remaining budget: the started batch still
    # completes, later batches are proactively abandoned
    recs = MergeScheduler(max_concurrency=10, time_budget_ms=15).run(
        [_batch(1, cost=10), _batch(2, cost=10)],
        lambda b: 20,  # actual cost exceeds the budget
    )
    assert recs[0]["outcome"] == OUTCOME_COMMITTED
    assert recs[1]["outcome"] == OUTCOME_ABANDONED
    assert recs[1]["reason"] == "budget-overrun"


def test_start_failed() -> None:
    def boom(b):
        raise BizError(Err.LEDGER_CORRUPT, "disk gone", batch_id=b.batch_id)

    recs = MergeScheduler(max_concurrency=2, time_budget_ms=100).run([_batch(1)], boom)
    assert recs[0]["outcome"] == OUTCOME_START_FAILED
    assert recs[0]["reason"] == Err.LEDGER_CORRUPT
    assert recs[0]["error"]["fields"]["batch_id"] == "b1"


def test_duplicate_batch_abandoned() -> None:
    def dup(b):
        raise BizError(Err.BATCH_DUPLICATE, "dup", batch_id=b.batch_id)

    recs = MergeScheduler(max_concurrency=2, time_budget_ms=100).run([_batch(1)], dup)
    assert recs[0]["outcome"] == OUTCOME_ABANDONED
    assert recs[0]["reason"] == "duplicate-committed"
