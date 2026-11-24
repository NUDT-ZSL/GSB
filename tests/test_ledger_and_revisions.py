from __future__ import annotations

from osl_forensics.ledger import Ledger
from osl_forensics.parser import parse_osl
from osl_forensics.policy import Policy
from osl_forensics.revisions import RevisionBatch, RevisionStore, compute_invalidation
from osl_forensics.writer import SegmentSpec, build_osl


def test_precheck_failure_does_not_mutate_ledger(tmp_path):
    db = tmp_path / "ledger.sqlite"
    good = parse_osl(build_osl([SegmentSpec("a", b"ok")]).data, "file")
    bad_blob = bytearray(build_osl([SegmentSpec("a", b"ok")]).data)
    bad_blob[0:4] = b"NOPE"
    bad = parse_osl(bytes(bad_blob), "file")

    with Ledger(db) as ledger:
        ledger.apply_result(good, run_id="run-good")
        before_states = ledger.comparable_states()
        before_events = ledger.verify_replay()["events"]
        summary = ledger.apply_result(bad, run_id="run-bad")
        after_states = ledger.comparable_states()
        after_events = ledger.verify_replay()["events"]

    assert summary["applied"] is False
    assert before_states == after_states
    assert before_events == after_events


def test_same_conclusion_replay_is_idempotent_and_policy_change_rebuilds(tmp_path):
    db = tmp_path / "ledger.sqlite"
    result = parse_osl(build_osl([SegmentSpec("a", b"ok")]).data, "file")

    with Ledger(db, Policy()) as ledger:
        ledger.apply_result(result, run_id="run-1")
        ledger.apply_result(result, run_id="run-2")
        states = ledger.list_states()
        assert len(states) == 1
        assert states[0]["state_generation"] == 1
        replay = ledger.verify_replay()
        assert replay["ok"]

    rebuilt_policy = Policy(allowed_segment_types=(1, 2, 3))
    with Ledger(db, rebuilt_policy) as ledger:
        assert ledger.policy_generation() == 2
        assert ledger.list_states() == []
        assert ledger.verify_replay()["events"] == 0


def test_revision_closure_is_transitive_and_order_independent():
    old = RevisionBatch(
        file_id="f",
        revision_id="r1",
        segments={"/raw": "h1", "/agg": "h2", "/report": "h3", "/extra": "h4"},
        edges=frozenset({("/agg", "/raw"), ("/report", "/agg")}),
    )
    new = RevisionBatch(
        file_id="f",
        revision_id="r2",
        segments={"/raw": "h1-changed", "/agg": "h2", "/report": "h3", "/extra": "h4"},
        edges=frozenset({("/report", "/agg"), ("/agg", "/raw")}),
    )
    first = compute_invalidation(old, new)

    assert first["changed_sources"][0]["segment_key"] == "/raw"
    assert first["recompute_segments"] == ["/agg", "/raw", "/report"]
    by_key = {item["segment_key"]: item for item in first["invalidations"]}
    assert by_key["/report"]["invalidation_sources"] == ["/raw"]
    assert by_key["/report"]["minimum_dependency_distance"] == 2

    shuffled_new = RevisionBatch(
        file_id="f",
        revision_id="r2",
        segments=dict(reversed(list(new.segments.items()))),
        edges=frozenset(reversed(list(new.edges))),
    )
    shuffled_old = RevisionBatch(
        file_id="f",
        revision_id="r1",
        segments=dict(reversed(list(old.segments.items()))),
        edges=frozenset(reversed(list(old.edges))),
    )
    shuffled_report = compute_invalidation(shuffled_old, shuffled_new)
    assert shuffled_report["recompute_segments"] == first["recompute_segments"]
    assert shuffled_report["invalidations"] == first["invalidations"]
    assert shuffled_report["changed_sources"] == first["changed_sources"]

    store_one = RevisionStore().add(new).add(old)
    store_two = RevisionStore().add(old).add(new)
    assert store_one.evaluate("f", "r1", "r2")["recompute_segments"] == [
        "/agg",
        "/raw",
        "/report",
    ]
    assert store_two.evaluate("f", "r1", "r2") == store_one.evaluate("f", "r1", "r2")
