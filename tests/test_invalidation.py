import pytest

from coord_workbench.errors import BizError, Err
from coord_workbench.invalidation import commit_policy, dependency_closure
from coord_workbench.persistence import Ledger


def test_dependency_closure(samples) -> None:
    views = samples.topo_index["views"]
    closure = dependency_closure(["node-b"], views)
    assert "node-b" in closure
    # node-a depends_on node-b in the seed topology
    assert "node-a" in closure
    assert "node-z" not in closure


def test_commit_policy_marks_recompute_and_reuse(samples, workdir) -> None:
    ledger = Ledger(workdir)
    views = samples.topo_index["views"]
    result = commit_policy(ledger, 1, {"rev": 1}, ["node-b"], views, samples.subs, clock=10)
    assert set(result["cache_views"]["recompute"]) | set(result["cache_views"]["reuse"]) == set(views)
    assert set(result["cache_views"]["recompute"]) & set(result["cache_views"]["reuse"]) == set()
    assert "node-b" in result["cache_views"]["recompute"]
    assert result["affected_domains"]


def test_unaffected_subscriptions_not_retriggered(samples, workdir) -> None:
    ledger = Ledger(workdir)
    views = samples.topo_index["views"]
    r1 = commit_policy(ledger, 1, {"rev": 1}, ["node-b"], views, samples.subs, clock=10)
    assert len(r1["callbacks"]["triggered"]) > 0
    log_after_v1 = len(ledger.callback_log())
    # node-a has no dependents and is no subscriber: nothing may re-trigger
    r2 = commit_policy(ledger, 2, {"rev": 2}, ["node-a"], views, samples.subs, clock=20)
    assert r2["callbacks"]["triggered"] == []
    assert len(r2["callbacks"]["skipped"]) == len(samples.subs)
    assert len(ledger.callback_log()) == log_after_v1


def test_policy_version_regress_rejected(samples, workdir) -> None:
    ledger = Ledger(workdir)
    views = samples.topo_index["views"]
    commit_policy(ledger, 5, {"rev": 5}, ["node-a"], views, samples.subs, clock=1)
    with pytest.raises(BizError) as ei:
        commit_policy(ledger, 4, {"rev": 4}, ["node-a"], views, samples.subs, clock=2)
    assert ei.value.code == Err.VERSION_REGRESS
