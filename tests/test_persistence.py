import pytest

from coord_workbench.engine import commit_batch, form_batches
from coord_workbench.errors import BizError, Err
from coord_workbench.persistence import Ledger


def test_batch_committed_once_per_epoch(samples, workdir) -> None:
    ledger = Ledger(workdir)
    _, batches = form_batches(samples)
    batch = batches[0]
    commit_batch(ledger, batch, samples.nodes_by_name)
    with pytest.raises(BizError) as ei:
        commit_batch(ledger, batch, samples.nodes_by_name)
    assert ei.value.code == Err.BATCH_DUPLICATE
    assert ei.value.fields["batch_id"] == batch.batch_id


def test_vote_counted_once(samples, workdir) -> None:
    ledger = Ledger(workdir)
    _, batches = form_batches(samples)
    for b in batches:
        commit_batch(ledger, b, samples.nodes_by_name)
    total = sum(len(ledger.get_votes(d["domain"])) for d in ledger.list_domain_states())
    assert total == len(samples.events)  # no double counting


def test_domain_state_atomic_fields(samples, workdir) -> None:
    ledger = Ledger(workdir)
    _, batches = form_batches(samples)
    for b in batches:
        commit_batch(ledger, b, samples.nodes_by_name)
    for st in ledger.list_domain_states():
        assert st["seq_watermark"] > 0
        assert st["clock_watermark"] > 0
        assert st["lease_generation"] >= 1
        assert st["leader_version"] >= 1
        assert st["chain_hash"]


def test_policy_version_regress_rejected(samples, workdir) -> None:
    ledger = Ledger(workdir)
    ledger.install_policy(2, {"k": 1}, clock=1)
    assert ledger.current_policy_version() == 2
    with pytest.raises(BizError) as ei:
        ledger.install_policy(2, {"k": 2}, clock=2)
    assert ei.value.code == Err.VERSION_REGRESS
    with pytest.raises(BizError):
        ledger.install_policy(1, {"k": 3}, clock=3)
    assert ledger.current_policy_version() == 2


def test_policy_pointer_swapped_atomically(samples, workdir) -> None:
    ledger = Ledger(workdir)
    ledger.install_policy(1, {"rev": 1}, clock=1)
    ledger.install_policy(2, {"rev": 2}, clock=2)
    assert ledger.current_policy_version() == 2


def test_callback_not_duplicated(samples, workdir) -> None:
    ledger = Ledger(workdir)
    assert ledger.record_callback("svc", "node-a", 1, 100) is True
    assert ledger.record_callback("svc", "node-a", 1, 100) is False
    assert len(ledger.callback_log()) == 1


def test_page_alignment(samples, workdir) -> None:
    ledger = Ledger(workdir)
    _, batches = form_batches(samples)
    for b in batches:
        commit_batch(ledger, b, samples.nodes_by_name)
    report = ledger.check_page_alignment()
    assert report["aligned"] is True
    assert report["size"] == report["page_size"] * report["page_count"]
