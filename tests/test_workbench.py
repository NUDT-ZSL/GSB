import json
import os
import threading

import numpy as np
import pytest

from mlworkbench.cache import Cache
from mlworkbench.config import ProjectionParams, WorkbenchConfig
from mlworkbench.engine import Engine, metrics_equal
from mlworkbench.errors import (
    BusinessError, E_CACHE_CORRUPT, E_DECISION_INVALID, E_INCONSISTENT,
    E_LABEL_UNKNOWN, E_METRIC_MISMATCH, E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY,
)
from mlworkbench.incremental import CommitLock
from mlworkbench.metrics import verify_metrics
from mlworkbench.pipeline import node_decide
from mlworkbench.registry import default_labelsets, default_projection

SEEDS = list(range(1, 13))


def make_engine(tmp_path, config_dict=None, seeds=SEEDS):
    cfg = WorkbenchConfig.from_dict(config_dict or {})
    return Engine(str(tmp_path / "ws"), cfg, seeds,
                  default_labelsets(), default_projection())


def test_determinism_byte_identical(tmp_path):
    e1 = make_engine(tmp_path / "a")
    e2 = make_engine(tmp_path / "b")
    r1, r2 = e1.run_full(), e2.run_full()
    assert r1.scores == r2.scores
    assert r1.decisions == r2.decisions
    assert r1.metrics == r2.metrics
    # artifact content hashes identical across independent workspaces
    assert sorted(r1.cache_keys.values()) == sorted(r2.cache_keys.values())


def test_cache_reuse_on_second_run(tmp_path):
    engine = make_engine(tmp_path)
    first = engine.run_full()
    assert all(st == "miss" for per in first.statuses.values() for st in per.values())
    second = engine.run_full()
    assert all(st == "reuse" for per in second.statuses.values() for st in per.values())
    assert second.metrics_status == "reuse"
    assert second.metrics == first.metrics


def test_explain_key_hit_and_miss(tmp_path):
    engine = make_engine(tmp_path)
    report = engine.run_full()
    key = report.cache_keys["decide:img-0003"]
    info = engine.explain_key(key)
    assert info["hit"] and info["components"]["node"] == "decide"
    assert engine.explain_key("0" * 64)["hit"] is False


def test_annotation_fix_only_recomputes_metrics(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    ann = engine.annotations["img-0003"]
    new_labels = set(ann["labels"]) ^ {"plant"}
    report = engine.fix_annotation("img-0003", new_labels, set(ann["uncertain"]))
    assert report.recompute == ["metrics"]
    assert report.affected["images"] == ["img-0003"]
    assert report.affected["labels"]["img-0003"] == ["plant"]
    fresh = engine.full_recompute_fresh()
    ok, _ = metrics_equal(report.metrics, fresh.metrics, engine.cfg.metric_tol)
    assert ok


def test_labelset_change_recomputes_suffix_only(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    report = engine.change_labelset("img-0005", 2)
    assert set(report.recompute) == {
        "score:img-0005", "decide:img-0005", "metrics"}
    assert report.statuses["preprocess"]["img-0005"] == "reuse"
    assert report.statuses["features"]["img-0005"] == "reuse"
    fresh = engine.full_recompute_fresh()
    ok, _ = metrics_equal(report.metrics, fresh.metrics, engine.cfg.metric_tol)
    assert ok


def test_projection_replacement_invalidates_features_downstream(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    report = engine.replace_projection(ProjectionParams(version=2, out_dim=32, seed=11))
    recomputed = set(report.recompute)
    for s in engine.samples:
        assert f"features:{s.image_id}" in recomputed
        assert f"score:{s.image_id}" in recomputed
        assert f"decide:{s.image_id}" in recomputed
        assert report.statuses["preprocess"][s.image_id] == "reuse"
    fresh = engine.full_recompute_fresh()
    ok, _ = metrics_equal(report.metrics, fresh.metrics, engine.cfg.metric_tol)
    assert ok


def test_uncertain_policies_change_counts(tmp_path):
    reports = {}
    for policy in ("ignore", "as_positive", "separate"):
        engine = make_engine(tmp_path / policy,
                             {"uncertain_policy": policy})
        reports[policy] = engine.run_full().metrics
    assert reports["ignore"]["counts"]["label_pairs"] != \
           reports["as_positive"]["counts"]["label_pairs"]
    assert reports["separate"]["uncertain_stats"]["total"] > 0
    assert reports["separate"]["counts"]["label_pairs"] == \
           reports["ignore"]["counts"]["label_pairs"]
    for rep in reports.values():
        assert rep["policy"]["uncertain"] in ("ignore", "as_positive", "separate")


def test_label_unknown_error(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    with pytest.raises(BusinessError) as exc:
        engine.fix_annotation("img-0003", {"not_a_label"}, set())
    assert exc.value.code == E_LABEL_UNKNOWN
    assert exc.value.details["image"] == "img-0003"
    assert exc.value.details["labels"] == ["not_a_label"]


def test_decision_invalid_error():
    ls = default_labelsets()[1]
    with pytest.raises(BusinessError) as exc:
        node_decide({"animal": 0.9, "ghost": 0.99}, ls, "img-x")
    # 'ghost' never reaches decided set via threshold dict; simulate directly
    if exc.value.code != E_DECISION_INVALID:
        pytest.fail("expected E_DECISION_INVALID")


def test_empty_and_multi_decisions_allowed(tmp_path):
    engine = make_engine(tmp_path)
    report = engine.run_full()
    ls_map = engine._labelsets_map()
    for img, decided in report.decisions.items():
        assert set(decided) <= set(ls_map[img].labels)
    assert any(len(d) != 1 for d in report.decisions.values())


def test_snapshot_rollback_rejected(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    engine.replace_projection(ProjectionParams(version=2, out_dim=32, seed=11))
    with pytest.raises(BusinessError) as exc:
        engine.replace_projection(ProjectionParams(version=1, out_dim=32, seed=11))
    assert exc.value.code == E_SNAPSHOT_ROLLBACK


def test_snapshot_corruption_detected(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    path = engine.metrics_store.path
    engine.fix_annotation("img-0003", {"plant"}, set())
    with open(path) as fh:
        doc = json.load(fh)
    doc["payload"]["hamming_loss"] = 0.12345  # tamper without fixing checksum
    with open(path, "w") as fh:
        json.dump(doc, fh)
    with pytest.raises(BusinessError) as exc:
        engine.metrics_store.load()
    assert exc.value.code == E_CACHE_CORRUPT
    assert exc.value.details["node"] == "metrics"


def test_stage_busy_no_blocking(tmp_path):
    lock = CommitLock(str(tmp_path / "staging" / "commit.lock"))
    errors = []

    def contender():
        try:
            with CommitLock(lock.path):
                pass
        except BusinessError as err:
            errors.append(err.code)

    with lock:
        t = threading.Thread(target=contender)
        t.start()
        t.join(timeout=5)
    assert errors == [E_STAGE_BUSY]


def test_resource_limit_error(tmp_path):
    engine = make_engine(tmp_path, {"limits": {"max_recompute_images": 2,
                                               "on_exceed": "error"}})
    engine.run_full()
    with pytest.raises(BusinessError) as exc:
        engine.replace_projection(ProjectionParams(version=2, out_dim=32, seed=11))
    assert exc.value.code == E_RESOURCE_LIMIT
    assert exc.value.details["violations"][0]["kind"] == "recompute_images"


def test_resource_limit_degrade_marks_categories(tmp_path):
    engine = make_engine(tmp_path, {"limits": {
        "max_recompute_images": 2, "max_feature_bytes": 8,
        "max_parallel_nodes": 1, "on_exceed": "degrade"}})
    engine.run_full()
    report = engine.replace_projection(ProjectionParams(version=2, out_dim=32, seed=11))
    assert set(report.degraded) == {
        "images_chunked", "features_streaming", "nodes_serialized"}
    fresh = engine.full_recompute_fresh()
    ok, _ = metrics_equal(report.metrics, fresh.metrics, engine.cfg.metric_tol)
    assert ok


def test_metric_mismatch_detected(tmp_path):
    engine = make_engine(tmp_path)
    report = engine.run_full()
    tampered = json.loads(json.dumps(report.metrics))
    tampered["micro"]["f1"] += 0.5
    labels = sorted({l for ls in engine._labelsets_map().values() for l in ls.labels})
    with pytest.raises(BusinessError) as exc:
        verify_metrics(tampered, report.details, labels, engine.cfg)
    assert exc.value.code == E_METRIC_MISMATCH


def test_metric_policy_required_for_export(tmp_path):
    engine = make_engine(tmp_path)
    report = engine.run_full()
    labels = sorted({l for ls in engine._labelsets_map().values() for l in ls.labels})
    with pytest.raises(BusinessError) as exc:
        verify_metrics({"micro": {}}, report.details, labels, engine.cfg)
    assert exc.value.code == E_METRIC_MISMATCH


def test_self_check_passes_and_detects_tampering(tmp_path):
    engine = make_engine(tmp_path)
    engine.run_full()
    engine.fix_annotation("img-0003", {"plant", "animal"}, set())
    results = engine.self_check()
    assert all(r["ok"] for r in results)
    # corrupt one cached artifact -> cross-check must fail
    key = engine.last_report.cache_keys["decide:img-0003"]
    info = engine.explain_key(key)
    artifact = os.path.join(engine.cache.root, info["artifact"])
    with open(artifact, "ab") as fh:
        fh.write(b"corruption")
    with pytest.raises(BusinessError) as exc:
        engine.self_check()
    assert exc.value.code == E_INCONSISTENT


def test_no_positive_label_policy_marked(tmp_path):
    engine = make_engine(tmp_path, {"no_positive_policy": "zero"})
    engine.run_full()
    # wipe all truth for one label -> support 0, flagged in report
    for img in engine.annotations:
        ann = engine.annotations[img]
        engine.annotations[img] = {
            "labels": [l for l in ann["labels"] if l != "vehicle"],
            "uncertain": [l for l in ann["uncertain"] if l != "vehicle"],
        }
    report = engine.run_full()
    assert "vehicle" in report.metrics["no_positive_labels"]
    assert report.metrics["per_label"]["vehicle"]["no_positive"] is True
    assert report.metrics["policy"]["no_positive"] == "zero"
