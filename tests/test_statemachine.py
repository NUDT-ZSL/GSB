from coord_workbench.models import Node
from coord_workbench.statemachine import LeaseStateMachine, decide_leader


def _node(name, priority):
    return Node(name=name, role="CANDIDATE", candidate_addr="127.0.0.1:1",
                lease_epoch=1, priority=priority, health="HEALTHY")


def test_decide_leader_by_priority_then_weight_then_name() -> None:
    nodes = {"a": _node("a", 10), "b": _node("b", 20), "c": _node("c", 20)}
    votes = {"a": {"weight": 9}, "b": {"weight": 1}, "c": {"weight": 2}}
    assert decide_leader(votes, [], nodes) == "c"  # same priority, higher weight
    assert decide_leader(votes, ["c"], nodes) == "b"  # c revoked


def test_lease_generation_only_increases() -> None:
    sm = LeaseStateMachine("d")
    t1 = sm.transition("node-a", 100)
    assert t1["changed"] and t1["lease_generation"] == 1
    t2 = sm.transition("node-a", 200)  # same leader: no bump
    assert not t2["changed"] and t2["lease_generation"] == 1
    t3 = sm.transition("node-b", 300)
    assert t3["lease_generation"] == 2
    assert t3["tombstone"] == {"old_leader": "node-a", "old_generation": 1, "invalidated_clock": 300}
    assert sm.generation == 2


def test_tombstone_recorded_on_every_switch() -> None:
    sm = LeaseStateMachine("d")
    sm.transition("a", 1)
    sm.transition("b", 2)
    sm.transition("c", 3)
    assert [t["old_leader"] for t in sm.tombstones] == ["a", "b"]
    assert sm.generation == 3
