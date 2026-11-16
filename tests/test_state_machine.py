import pytest

from app.store import StateError, Store


def test_legal_path(store):
    tid = store.create_task({})
    store.transition(tid, "running")
    store.transition(tid, "paused")
    store.transition(tid, "running")
    store.transition(tid, "failed")
    store.transition(tid, "running")
    store.transition(tid, "succeeded")
    assert store.get_task(tid)["status"] == "succeeded"
    states = [(e["from_state"], e["to_state"]) for e in store.summary(tid)["events"]]
    assert states == [(None, "pending"), ("pending", "running"), ("running", "paused"),
                      ("paused", "running"), ("running", "failed"), ("failed", "running"),
                      ("running", "succeeded")]


@pytest.mark.parametrize("frm_path,bad_to", [
    ([], "succeeded"),
    ([], "paused"),
    (["running"], "pending"),
    (["running", "succeeded"], "running"),
    (["running", "paused"], "succeeded"),
    (["running", "paused"], "failed"),
])
def test_illegal_transition_rejected_with_e_state(store, frm_path, bad_to):
    tid = store.create_task({})
    for to in frm_path:
        store.transition(tid, to)
    with pytest.raises(StateError) as exc:
        store.transition(tid, bad_to)
    assert exc.value.code == "E_STATE"
    # 状态未被破坏
    expected = frm_path[-1] if frm_path else "pending"
    assert store.get_task(tid)["status"] == expected


def test_recover_orphans(store):
    tid = store.create_task({})
    store.transition(tid, "running")
    assert store.recover_orphans() == [tid]
    assert store.get_task(tid)["status"] == "paused"
    store.transition(tid, "running")  # 可恢复
