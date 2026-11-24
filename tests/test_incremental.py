from pdre.config import EngineConfig
from pdre.codes import BusinessCode as Codes
from pdre.engine import analyze
from pdre.generator import generate_sample
from pdre.incremental import IncrementalEngine
from pdre.models import LineageRecord, SourceRef
from pdre.pipeline import load_input_bundle
from pdre.verification import run_shuffled_batches

from factories import event, policy


def lineage(child: str, parent: str | None, session: str = "s1", seq: int = 1) -> LineageRecord:
    return LineageRecord(
        process_guid=child,
        parent_process_guid=parent,
        session_id=session,
        source=SourceRef("lineage", seq, "lineage.jsonl"),
    )


def test_generated_sample_is_order_and_batch_invariant(tmp_path):
    sample_dir = tmp_path / "sample"
    generate_sample(sample_dir)
    bundle = load_input_bundle(sample_dir)
    result = run_shuffled_batches(bundle, iterations=12, config=EngineConfig(concurrency=2))
    assert result["all_matched"] is True
    assert len(result["unique_observed_digests"]) == 1


def test_lineage_fork_split_and_reverse_merge_match_full_recompute():
    engine = IncrementalEngine(EngineConfig(concurrency=1))
    fork_b = lineage("f", "rb-parent", "session-b", seq=8)
    initial_lineage = [
        lineage("ra", None, seq=1),
        lineage("ra-parent", "ra", seq=2),
        lineage("rb", None, seq=3),
        lineage("rb-parent", "rb", seq=4),
        lineage("f", "ra-parent", "session-a", seq=5),
        lineage("d", "f", seq=6),
    ]
    events = [
        event(seq=1, process="f", parent="ra-parent"),
        event(seq=2, process="d", parent="f"),
    ]

    first = engine.apply_batch(
        batch_id="initial",
        events=events,
        lineage=initial_lineage,
        verify=True,
    )
    assert first["incremental"]["consistency"] == "matched"
    assert first["forks"] == []
    assert first["units"][0]["unit_type"] == "root"

    forked = engine.apply_batch(batch_id="fork", lineage=[fork_b], verify=True)
    assert forked["incremental"]["consistency"] == "matched"
    assert len(forked["forks"]) == 1
    assert forked["forks"][0]["candidate_root_guids"] == ["ra", "rb"]
    fork_unit = next(unit for unit in forked["units"] if unit["unit_type"] == "fork")
    assert fork_unit["event_count"] == 2

    engine.remove_lineage(fork_b.uid)
    merged = engine.recompute(batch_id="reverse-merge", verify=True)
    assert merged["incremental"]["consistency"] == "matched"
    assert merged["forks"] == []
    assert all(unit["unit_type"] != "fork" for unit in merged["units"])

    full = analyze(engine.events, engine.lineage_records, engine.policy_records, engine.config)
    assert merged["semantic_digest"] == full["semantic_digest"]


def test_policy_revision_and_tombstone_invalidate_dependency_units():
    digest = "sha256:" + "e" * 64
    engine = IncrementalEngine(EngineConfig(concurrency=1))
    ev = event(process="root1", parent=None, policy_key="pdre:k", digest=digest)
    old_policy = policy(key="pdre:k", generation=1, action="AUDIT", digest=digest, seq=1)
    root_decl = lineage("root1", None, seq=99)

    first = engine.apply_batch(
        batch_id="p1",
        events=[ev],
        lineage=[root_decl],
        policies=[old_policy],
        verify=True,
    )
    assert first["units"][0]["decision"]["action_code"] == "AUDIT"

    new_policy = policy(key="pdre:k", generation=2, action="BLOCK", digest=digest, seq=2)
    revised = engine.apply_batch(batch_id="p2", policies=[new_policy], verify=True)
    assert revised["incremental"]["consistency"] == "matched"
    assert revised["units"][0]["decision"]["action_code"] == "BLOCK"
    assert "unit:root:root1" in revised["incremental"]["recomputed_unit_ids"]

    tombstone = policy(
        key="pdre:k",
        generation=3,
        tombstone=True,
        start_ms=5_000,
        end_ms=None,
        seq=3,
    )
    removed = engine.apply_batch(batch_id="p3", policies=[tombstone], verify=True)
    assert removed["incremental"]["consistency"] == "matched"
    assert removed["units"][0]["decision"] is None
    assert len(removed["tombstone_overrides"]) == 1


def test_rejudge_mismatch_returns_business_code():
    digest = "sha256:" + "f" * 64
    engine = IncrementalEngine(EngineConfig(concurrency=1))
    ev = event(process="root1", parent=None, digest=digest)
    root_decl = lineage("root1", None)
    pol = policy(digest=digest)
    engine.apply_batch(
        batch_id="b1",
        events=[ev],
        lineage=[root_decl],
        policies=[pol],
        verify=True,
    )
    unit_id = "unit:root:root1"
    engine.unit_cache[unit_id]["decision"]["action_code"] = "TAMPERED"

    report = engine.recompute(batch_id="tamper-check", verify=True)
    assert report["summary"]["rejudge_mismatch_count"] == 1
    assert report["rejudge_errors"][0]["code"] == Codes.REJUDGE_MISMATCH
    assert report["incremental"]["consistency"] == "mismatch"
