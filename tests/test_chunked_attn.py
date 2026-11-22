"""End-to-end tests for the chunked attention component."""
import copy
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from chunked_attn import consistency, core, samples, stability, validation
from chunked_attn.api import ChunkedAttentionOperator
from chunked_attn.config import StabilityConfig
from chunked_attn.errors import (BizError, E_BLOCK_MISMATCH,
                                 E_DIM_MISMATCH, E_DUPLICATE_CASE_ID,
                                 E_HEADS_MISMATCH, E_INCONSISTENT,
                                 E_INVALID_BLOCK, E_INVALID_CAUSAL,
                                 E_LATENCY_BUDGET, E_SNAPSHOT_CORRUPT,
                                 E_SNAPSHOT_ROLLBACK, E_STABILITY_VIOLATION)
from chunked_attn.planner import plan_blocks
from chunked_attn.ready import BlockReadySession
from chunked_attn.selfcheck import self_check
from chunked_attn.snapshot import SnapshotStore

CFG = StabilityConfig()


def _case(**over):
    rec = copy.deepcopy(samples.SAMPLE_CASES[0])
    rec.update(over)
    return rec


def _f64(rec):
    q, k, v = samples.gen_arrays(rec)
    return (q.astype(np.float64), k.astype(np.float64),
            v.astype(np.float64))


def test_determinism_same_seed_same_config():
    rec = _case()
    q, k, v = _f64(rec)
    a = core.run_chunked(q, k, v, 16, 16, True, CFG)
    b = core.run_chunked(q, k, v, 16, 16, True, CFG)
    assert a.output_checksum() == b.output_checksum()
    assert a.stats_checksum() == b.stats_checksum()


@pytest.mark.parametrize("causal", [True, False])
@pytest.mark.parametrize("q_blk,kv_blk", [(16, 16), (8, 32), (64, 8),
                                          (7, 13), (64, 64)])
def test_chunked_matches_whole_byte_exact(causal, q_blk, kv_blk):
    rec = _case(causal=causal)
    q, k, v = _f64(rec)
    chunked = core.run_chunked(q, k, v, q_blk, kv_blk, causal, CFG)
    whole = core.run_whole(q, k, v, kv_blk, causal, CFG)
    verdict = consistency.adjudicate(chunked, whole, case_id="t")
    assert verdict["byte_identical"]


def test_causal_blocks_future_positions():
    rec = _case(causal=True)
    q, k, v = _f64(rec)
    base = core.run_chunked(q, k, v, 16, 16, True, CFG).output
    k2, v2 = k.copy(), v.copy()
    k2[:, 32:, :] += 100.0
    v2[:, 32:, :] -= 100.0
    after = core.run_chunked(q, k2, v2, 16, 16, True, CFG).output
    np.testing.assert_array_equal(base[:, :32], after[:, :32])


def test_ready_query_partial_and_first_block_latency():
    rec = _case()
    plan = plan_blocks(rec)
    q, k, v = _f64(rec)
    session = BlockReadySession(rec, q, k, v, plan, CFG)
    seen = []
    orig = session._on_block

    def spy(qi, qs, qe, t0):
        orig(qi, qs, qe, t0)
        seen.append(dict(session.query_ready()["blocks"][-1]))

    session._on_block = spy
    session.run()
    assert len(seen) == len(session.ready_blocks)
    first = session.query_ready(up_to=0)
    assert len(first["blocks"]) == 1
    blk = first["blocks"][0]
    assert (blk["q_start"], blk["q_end"]) == (0, plan.q_block_len)
    assert blk["output"].shape == (rec["n_heads"], plan.q_block_len,
                                   rec["head_dim"])
    assert session.first_block_latency_ms is not None
    est = first["estimate"]
    assert est["q_block_len"] == plan.q_block_len
    assert est["kv_block_count"] >= 1
    assert est["score_elems_per_block"] == plan.q_block_len * plan.kv_block_len


def test_latency_budget_error_lists_completed_blocks():
    rec = _case(latency_budget_ms=1e-9)
    plan = plan_blocks(rec)
    q, k, v = _f64(rec)
    session = BlockReadySession(rec, q, k, v, plan, CFG)
    with pytest.raises(BizError) as ei:
        session.run()
    assert ei.value.code == E_LATENCY_BUDGET
    assert "completed_blocks" in ei.value.details
    assert ei.value.stage == "compute"


def test_block_mismatch_reports_first_deviation():
    rec = _case()
    q, k, v = _f64(rec)
    chunked = core.run_chunked(q, k, v, 16, 16, True, CFG)
    whole = core.run_whole(q, k, v, 16, True, CFG)
    chunked.output[1, 40, 3] += 1.0
    with pytest.raises(BizError) as ei:
        consistency.adjudicate(chunked, whole, case_id="t")
    err = ei.value
    assert err.code == E_BLOCK_MISMATCH
    assert err.details["first_deviation"] == {"head": 1, "row": 40, "col": 3}
    assert err.details["q_block_idx"] == 40 // 16
    assert err.details["max_merge_deviation"] == pytest.approx(1.0)


def test_planner_records_candidates_and_priority():
    rec = _case()
    plan = plan_blocks(rec)
    assert plan.candidates, "candidates must be recorded"
    assert plan.priority_rule.startswith("P1")
    assert plan.rationale
    feasible = [c for c in plan.candidates if c.feasible]
    chosen = [c for c in plan.candidates
              if c.q_block_len == plan.q_block_len
              and c.kv_block_len == plan.kv_block_len][0]
    if feasible:
        best_lat = min(c.est_first_block_ms for c in feasible)
        assert chosen.est_first_block_ms == best_lat


def test_planner_infeasible_budget_flagged():
    rec = _case(latency_budget_ms=1e-12)
    plan = plan_blocks(rec)
    assert not plan.feasible
    best = min(c.est_first_block_ms for c in plan.candidates)
    chosen = [c for c in plan.candidates
              if c.q_block_len == plan.q_block_len
              and c.kv_block_len == plan.kv_block_len][0]
    assert chosen.est_first_block_ms == best


def test_denominator_reconstruction_within_tolerance():
    rec = _case()
    q, k, v = _f64(rec)
    chunked = core.run_chunked(q, k, v, 16, 16, True, CFG)
    whole = core.run_whole(q, k, v, 16, True, CFG)
    result = stability.verify_run(chunked, whole, CFG, case_id="t")
    assert result["max_reconstruction_deviation"] <= CFG.tolerance


def test_stability_violation_pinpoints_block():
    rec = _case()
    q, k, v = _f64(rec)
    chunked = core.run_chunked(q, k, v, 16, 16, True, CFG)
    whole = core.run_whole(q, k, v, 16, True, CFG)
    chunked.q_blocks[0][1].kv_stats[0].l_blk += 0.5
    with pytest.raises(BizError) as ei:
        stability.verify_run(chunked, whole, CFG, case_id="t")
    assert ei.value.code == E_STABILITY_VIOLATION
    assert ei.value.details["q_block_idx"] == 1
    assert ei.value.details["head"] == 0


def test_rounding_modes_and_subnormal_protection():
    x = np.array([0.1, -0.1, 1.0 + 2**-25, -1.0 - 2**-25])
    nearest = core.apply_rounding(x, "float32", "nearest_even")
    toward = core.apply_rounding(x, "float32", "toward_zero")
    assert nearest.dtype == np.float32 == toward.dtype
    assert np.all(np.abs(toward.astype(np.float64)) <= np.abs(x) + 1e-12)
    cfg = StabilityConfig(subnormal_protection=True)
    tiny = np.array([np.finfo(np.float64).tiny / 2, 1.0])
    flushed = core._flush_subnormal(tiny, cfg)
    assert flushed[0] == 0.0 and flushed[1] == 1.0


def test_snapshot_commit_load_and_rollback(tmp_path):
    store = SnapshotStore(str(tmp_path))
    meta = store.commit("block_plan", "c1", 1, {"q": 16})
    snap = store.load("block_plan", "c1")
    assert snap["version"] == 1 and snap["checksum"] == meta["checksum"]
    store.commit("block_plan", "c1", 2, {"q": 32})
    assert store.load("block_plan", "c1")["payload"] == {"q": 32}
    with pytest.raises(BizError) as ei:
        store.commit("block_plan", "c1", 2, {"q": 64})
    assert ei.value.code == E_SNAPSHOT_ROLLBACK
    with pytest.raises(BizError) as ei:
        store.commit("block_plan", "c1", 1, {"q": 64})
    assert ei.value.code == E_SNAPSHOT_ROLLBACK


def test_snapshot_corrupt_detected(tmp_path):
    store = SnapshotStore(str(tmp_path))
    store.commit("ready_list", "c1", 1, {"blocks": [1]})
    path = store._path("ready_list", "c1")
    with open(path) as fh:
        doc = json.load(fh)
    doc["payload"]["blocks"] = [2]
    with open(path, "w") as fh:
        json.dump(doc, fh)
    with pytest.raises(BizError) as ei:
        store.load("ready_list", "c1")
    assert ei.value.code == E_SNAPSHOT_CORRUPT


def test_validation_codes():
    rec = _case()
    with pytest.raises(BizError) as ei:
        validation.validate_manifest([rec, dict(rec)])
    assert ei.value.code == E_DUPLICATE_CASE_ID
    assert ei.value.field == "case_id"
    with pytest.raises(BizError) as ei:
        validation.validate_record(_case(q_block_len=0))
    assert ei.value.code == E_INVALID_BLOCK
    assert ei.value.field == "q_block_len"
    with pytest.raises(BizError) as ei:
        validation.validate_record(_case(kv_block_len=-3))
    assert ei.value.code == E_INVALID_BLOCK
    with pytest.raises(BizError) as ei:
        validation.validate_record(_case(causal=1))
    assert ei.value.code == E_INVALID_CAUSAL
    q, k, v = samples.gen_arrays(rec)
    with pytest.raises(BizError) as ei:
        validation.validate_arrays(rec, q, k, v[:-1])
    assert ei.value.code == E_HEADS_MISMATCH
    with pytest.raises(BizError) as ei:
        validation.validate_arrays(rec, q, k[:, :-1], v)
    assert ei.value.code == E_DIM_MISMATCH


def test_operator_entries_and_selfcheck(tmp_path):
    op = ChunkedAttentionOperator(str(tmp_path))
    rec = _case()
    q, k, v = samples.gen_arrays(rec)
    result = op.compute_chunked(rec, q, k, v)
    verdict = op.adjudicate_consistency(rec, q, k, v)
    assert verdict["byte_identical"]
    ready = op.query_ready_blocks(rec, q, k, v)
    assert ready["complete"]
    op.snapshots.commit("ready_list", rec["case_id"], 1,
                        {"blocks": [b["q_block_idx"]
                                    for b in ready["blocks"]]})
    meta = op.query_snapshot("ready_list", rec["case_id"])
    assert meta["exists"] and meta["version"] == 1
    sc = self_check([{
        "case_id": rec["case_id"],
        "chunked_run": result["chunked_run"],
        "whole_run": result["whole_run"],
        "ready_blocks": result["session"].ready_blocks,
    }], CFG)
    assert sc["verdict"] == "OK"


def test_selfcheck_detects_tampered_ready_list():
    rec = _case()
    q, k, v = _f64(rec)
    run = core.run_chunked(q, k, v, 16, 16, True, CFG)
    whole = core.run_whole(q, k, v, 16, True, CFG)
    bad = [{"q_block_idx": 0, "q_start": 0, "q_end": 16,
            "output_checksum": "0" * 64}]
    with pytest.raises(BizError) as ei:
        self_check([{"case_id": "t", "chunked_run": run,
                     "whole_run": whole, "ready_blocks": bad}], CFG)
    assert ei.value.code == E_INCONSISTENT
