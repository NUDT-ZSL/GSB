from __future__ import annotations

import copy
from collections import Counter

from diagnostic_engine.engine import DiagnosticEngine


def test_second_run_reuses_every_node(engine):
    engine.run_all()
    second = engine.run_all()
    counts = Counter(action["action"] for action in second["actions"])
    assert counts["reused"] == 8 * 6
    assert counts["recomputed"] == 0


def test_reduction_change_reuses_upstream_nodes(config, records, workdir):
    engine = DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(records), workdir=workdir)
    engine.run_all()
    engine.config["reduction"]["active"] = "pca3"
    report = engine.run_incremental({"type": "reduction_changed"})
    counts = Counter((action["node"], action["action"]) for action in report["actions"])

    for node in ("preprocess", "cross_expand", "standardize"):
        assert counts[(node, "reused")] == 8
        assert counts[(node, "recomputed")] == 0
    for node in ("reduce", "predict", "error"):
        assert counts[(node, "recomputed")] == 8
    assert report["incremental"]["recomputed_count"] == 24


def test_cross_change_reuses_preprocess_only(config, records, workdir):
    engine = DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(records), workdir=workdir)
    engine.run_all()
    engine.config["cross"]["slots"].append(
        {"name": "extra_pair", "arity": 2, "variables": ["x07", "x08"], "ops": ["product"]}
    )
    report = engine.run_incremental({"type": "cross_slot_changed", "slot": "extra_pair"})
    counts = Counter((action["node"], action["action"]) for action in report["actions"])

    assert counts[("preprocess", "reused")] == 8
    assert counts[("cross_expand", "recomputed")] == 8
    assert counts[("standardize", "recomputed")] == 8
    assert counts[("reduce", "recomputed")] == 8


def test_single_variable_change_recomputes_window_and_error_suffix(engine):
    engine.run_all()
    target_index = next(
        i
        for i, record in enumerate(engine.records)
        if record["src_name"] == "x03" and 6400 <= record["obs_time_ms"] < 9600
    )
    target_v_idx = engine.records[target_index]["v_idx"]
    engine.records[target_index]["values"][0] += 2.5

    plan = engine.plan_invalidation(
        {"type": "variable_reorganized", "variable": "x03", "v_idx": target_v_idx}
    )
    assert target_v_idx in plan["affected_v_idx"]

    report = engine.run_incremental({"type": "variable_reorganized", "variable": "x03"})
    counts = Counter((action["node"], action["action"]) for action in report["actions"])
    assert counts[("preprocess", "recomputed")] == 1
    assert counts[("preprocess", "reused")] == 7
    assert counts[("predict", "recomputed")] == 1
    assert counts[("predict", "reused")] == 7
    # Changed window is index 2; windows 2..7 errors are recomputed.
    assert counts[("error", "recomputed")] == 6
    assert counts[("error", "reused")] == 2


def test_window_slide_adds_only_new_window(config, generated_nine_windows, workdir):
    nine_config, nine_records = generated_nine_windows
    initial_records = [r for r in nine_records if r["obs_time_ms"] < 25600]
    engine = DiagnosticEngine(copy.deepcopy(config), copy.deepcopy(initial_records), workdir=workdir)
    engine.run_all()

    new_records = [r for r in nine_records if r["obs_time_ms"] >= 25600]
    engine.records.extend(copy.deepcopy(new_records))
    engine.config["time"]["windows"] = 9
    plan = engine.plan_invalidation({"type": "window_slide", "from_window_index": 8})
    assert plan["affected_windows"] == ["w008-25600-28800"]

    report = engine.run_incremental({"type": "window_slide", "from_window_index": 8})
    assert sum(1 for a in report["actions"] if a["window_index"] == 8 and a["action"] == "recomputed") == 6
    for window_index in range(8):
        window_actions = [a for a in report["actions"] if a["window_index"] == window_index]
        assert window_actions
        assert all(a["action"] == "reused" for a in window_actions)
