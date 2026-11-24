from pdre.config import EngineConfig
from pdre.engine import analyze
from pdre.models import LineageRecord, SourceRef
from pdre.policies import build_policy_view

from factories import event, policy


def root_lineage(guid: str = "root1") -> LineageRecord:
    return LineageRecord(
        process_guid=guid,
        parent_process_guid=None,
        session_id="s-root",
        source=SourceRef("lineage", 1, "lineage.jsonl"),
    )


def test_policy_conflict_and_tombstone_override_are_stable():
    digest = "sha256:" + "a" * 64
    old = policy(key="pdre:k", generation=1, action="AUDIT", seq=1, format_name="v1")
    new = policy(key="pdre:k", generation=2, action="BLOCK", seq=2)
    tomb = policy(key="pdre:gone", generation=2, tombstone=True, seq=3)
    old_gone = policy(key="pdre:gone", generation=1, action="AUDIT", seq=4, format_name="v1")

    view = build_policy_view([old, new, tomb, old_gone])
    conflict = next(item for item in view.conflicts if item["policy_key"] == "pdre:k")
    assert conflict["conflict_fields"] == ["action_code"]
    assert conflict["generations"] == [1, 2]
    assert len(view.tombstone_overrides) == 1
    assert all(item.policy_key != "pdre:gone" for item in view.policies)


def test_unit_adjudication_marks_suppressed_hits():
    digest = "sha256:" + "b" * 64
    events = [event(seq=2, process="root1", parent=None, observed_ms=100, digest=digest)]
    policies = [
        policy(key="pdre:low", generation=1, action="AUDIT", digest=digest, start_ms=0, end_ms=1000),
        policy(key="pdre:mid", generation=2, action="AUDIT", digest=digest, start_ms=0, end_ms=1000),
        policy(key="pdre:win", generation=2, action="BLOCK", digest=digest, start_ms=0, end_ms=1000),
    ]
    report = analyze(events, [root_lineage()], policies, EngineConfig(concurrency=1))
    unit = report["units"][0]

    assert unit["unit_type"] == "root"
    assert unit["decision"]["policy_key"] == "pdre:win"
    assert [hit["status"] for hit in unit["hits"]] == ["winner", "suppressed", "suppressed"]
    assert all(hit["suppression_reason"] == "lost_stable_adjudication" for hit in unit["hits"][1:])


def test_time_window_is_half_open():
    digest = "sha256:" + "c" * 64
    events = [
        event(seq=1, observed_ms=10, digest=digest),
        event(seq=2, observed_ms=20, digest=digest),
    ]
    policies = [policy(digest=digest, start_ms=10, end_ms=20)]
    report = analyze(events, [], policies, EngineConfig(concurrency=1))
    unit = report["units"][0]
    assert [hit["event_seq"] for hit in unit["hits"]] == [1]


def test_semantic_report_is_order_independent_for_policy_list():
    digest = "sha256:" + "d" * 64
    events = [event(digest=digest)]
    policies = [policy(key="pdre:a", digest=digest), policy(key="pdre:b", digest=digest)]
    first = analyze(events, [], policies, EngineConfig(concurrency=1))
    second = analyze(events, [], list(reversed(policies)), EngineConfig(concurrency=1))
    assert first["semantic_digest"] == second["semantic_digest"]
