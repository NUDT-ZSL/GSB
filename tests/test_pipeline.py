"""安全策略交互一致性验证管线测试集（业务要求 8）。

覆盖：一致、摘要不符、陈旧版本、超时、队列超限、乱序返回、
并发度差异、检查点中断恢复、版本重建与文件锁冲突。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import errors, samplegen
from pipeline.checkpoint import CheckpointStore, FileLock, LockError
from pipeline.engine import EngineConfig, SimulatedCrash, _projection, run_pipeline
from pipeline.normalize import normalize_policy, normalize_requests
from pipeline.service import LocalPolicyService


@pytest.fixture()
def samples(tmp_path):
    sample_dir = tmp_path / "samples"
    samplegen.generate_samples(str(sample_dir))
    decl = json.loads((sample_dir / "policy.json").read_text(encoding="utf-8"))
    manifest = json.loads((sample_dir / "requests.json").read_text(encoding="utf-8"))
    truth = json.loads((sample_dir / "truth.json").read_text(encoding="utf-8"))
    return {
        "dir": sample_dir,
        "decl": decl,
        "manifest": manifest,
        "truth": truth,
        "truth_by_id": {e["request_id"]: e for e in truth["requests"]},
    }


def _run(samples, tmp_path, name="work", config=None, service=None):
    workdir = tmp_path / name
    svc = service if service is not None else samplegen.make_service()
    cfg = config if config is not None else EngineConfig()
    report = run_pipeline(samples["decl"], samples["manifest"], svc, cfg,
                          str(workdir))
    return report, svc, workdir


def test_normalize_rejects_invalid_entries():
    decl = {
        "policy_version": "v1",
        "rule_groups": [
            {"group_id": "g1", "rules": [
                {"rule_id": "r1", "condition": {"lower": 0, "upper": 5},
                 "action": "allow", "priority": 1},
                {"rule_id": "r1", "condition": {"lower": 6, "upper": 9},
                 "action": "deny", "priority": 2},
                {"rule_id": "r2", "condition": {"lower": 9, "upper": 3},
                 "action": "deny", "priority": 2},
                {"rule_id": "r3",
                 "condition": {"lower": 0, "upper": 1,
                               "lower_endpoint": "sideways"},
                 "action": "deny", "priority": 2},
                {"rule_id": "r4", "condition": {"lower": 0, "upper": 1},
                 "action": "deny", "priority": "high"},
                {"rule_id": "r5", "condition": {"lower": 0, "upper": 1},
                 "action": "deny", "priority": 3},
            ]},
            {"group_id": "g1", "rules": []},
        ],
    }
    groups, version, rejects = normalize_policy(decl)
    assert version == "v1"
    codes = [r["code"] for r in rejects]
    assert codes == [errors.E_DUP_RULE, errors.E_RANGE, errors.E_ENDPOINT,
                     errors.E_PRIORITY, errors.E_DUP_GROUP]
    assert rejects[0]["index"] == {"group_index": 0, "rule_index": 1}
    assert rejects[4]["index"] == 1
    assert [r["rule_id"] for r in groups["g1"]["rules"]] == ["r1", "r5"]

    manifest = {"requests": [
        {"request_id": "q1", "group_id": "g1", "deadline_ms": 100,
         "expected_summary": {"conflicts": [], "shadowed": []}},
        {"request_id": "q1", "group_id": "g1", "deadline_ms": 100,
         "expected_summary": {"conflicts": [], "shadowed": []}},
        {"request_id": "q2", "group_id": "gX", "deadline_ms": 100,
         "expected_summary": {"conflicts": [], "shadowed": []}},
        {"request_id": "q3", "group_id": "g1", "deadline_ms": 0,
         "expected_summary": {"conflicts": [], "shadowed": []}},
        {"request_id": "q4", "group_id": "g1", "deadline_ms": 100,
         "expected_summary": {"conflicts": "bad"}},
    ]}
    requests, req_rejects = normalize_requests(manifest, groups)
    assert [r["code"] for r in req_rejects] == [
        errors.E_DUP_REQUEST, errors.E_UNKNOWN_GROUP,
        errors.E_DEADLINE, errors.E_SUMMARY]
    assert [r["index"] for r in req_rejects] == [1, 2, 3, 4]
    assert [r["request_id"] for r in requests] == ["q1"]


def test_verdicts_match_truth(samples, tmp_path):
    report, _, _ = _run(samples, tmp_path)
    by_id = {r["request_id"]: r for r in report["requests"]}
    for rid, truth in samples["truth_by_id"].items():
        rec = by_id[rid]
        assert rec["status"] == truth["expected_status"], rid
        assert rec["timed_out"] == truth["expect_timed_out"], rid
        expected = truth["expected_summary"]
        local = rec["local_summary"]
        assert local["conflicts"] == expected["conflicts"], rid
        assert local["shadowed"] == expected["shadowed"], rid


def test_mismatch_records_diff_fields(samples, tmp_path):
    report, _, _ = _run(samples, tmp_path)
    by_id = {r["request_id"]: r for r in report["requests"]}
    rec = by_id["q4"]
    assert rec["status"] == errors.E_MISMATCH
    assert rec["diff_fields"] == ["conflicts"]
    assert rec["adopted"] is False
    assert report["mismatches"] == ["q4"]


def test_stale_rejected_and_not_adopted(samples, tmp_path):
    report, _, _ = _run(samples, tmp_path)
    by_id = {r["request_id"]: r for r in report["requests"]}
    rec = by_id["q5"]
    assert rec["status"] == errors.E_STALE
    assert rec["adopted"] is False
    assert rec["received_summary"]["summary_version"] == "v0"
    assert rec["local_summary"]["summary_version"] == "v1"
    assert report["stale_rejected"] == ["q5"]


def test_timeout_recorded_with_excess_and_batch_continues(samples, tmp_path):
    report, _, _ = _run(samples, tmp_path)
    by_id = {r["request_id"]: r for r in report["requests"]}
    rec = by_id["q6"]
    assert rec["timed_out"] is True
    assert rec["status"] == errors.OK
    assert rec["latency_ms"] > rec["deadline_ms"]
    assert rec["excess_ms"] == pytest.approx(
        rec["latency_ms"] - rec["deadline_ms"], abs=0.01)
    assert report["timeouts"] == ["q6"]
    assert report["counts"]["total"] == 8
    assert report["counts"]["ok"] == 6


def test_service_sync_markers_match_truth(samples, tmp_path):
    _, service, _ = _run(samples, tmp_path)
    log_by_group = {e["group_id"]: e for e in service.query_log()}
    for truth in samples["truth"]["requests"]:
        marker = truth["sync_marker"]
        entry = log_by_group[truth["group_id"]]
        assert entry["applied_delay_ms"] == marker["total_delay_ms"]


def test_hard_timeout_marks_e_timeout(samples, tmp_path):
    config = EngineConfig(workers=4, hard_timeout_ms=100)
    report, _, _ = _run(samples, tmp_path, config=config)
    by_id = {r["request_id"]: r for r in report["requests"]}
    assert by_id["q6"]["status"] == errors.E_TIMEOUT
    assert "q6" in report["timeouts"]
    assert by_id["q1"]["status"] == errors.OK
    assert report["counts"]["total"] == 8


def test_queue_limit_rejects_overflow(samples, tmp_path):
    config = EngineConfig(workers=4, queue_limit=5)
    report, _, _ = _run(samples, tmp_path, config=config)
    assert report["counts"]["queue_rejected"] == 3
    assert report["queue_rejected"] == ["q6", "q7", "q8"]
    by_id = {r["request_id"]: r for r in report["requests"]}
    for rid in ("q6", "q7", "q8"):
        assert by_id[rid]["status"] == errors.E_QUEUE_FULL
    assert by_id["q1"]["status"] == errors.OK
    assert by_id["q4"]["status"] == errors.E_MISMATCH


@pytest.mark.parametrize("order_mode", ["normal", "reverse", "shuffle"])
@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_concurrency_equivalence(samples, tmp_path, workers, order_mode):
    svc_cfg = dict(samplegen.SERVICE_CONFIG, order_mode=order_mode)
    service = samplegen.make_service(svc_cfg)
    config = EngineConfig(workers=workers)
    report, _, _ = _run(samples, tmp_path,
                        name="w%s-%s" % (workers, order_mode),
                        config=config, service=service)
    baseline_service = samplegen.make_service(
        dict(samplegen.SERVICE_CONFIG, order_mode="normal"))
    baseline_cfg = EngineConfig(workers=1)
    baseline, _, _ = _run(samples, tmp_path,
                          name="baseline-%s" % order_mode,
                          config=baseline_cfg, service=baseline_service)
    assert _projection(report) == _projection(baseline)
    assert [r["request_id"] for r in report["requests"]] == sorted(
        r["request_id"] for r in report["requests"])


def test_checkpoint_crash_and_resume(samples, tmp_path):
    workdir = tmp_path / "crashy"
    with pytest.raises(SimulatedCrash):
        run_pipeline(samples["decl"], samples["manifest"],
                     samplegen.make_service(),
                     EngineConfig(workers=2, crash_after=3), str(workdir))
    assert not (workdir / "report.json").exists()
    state = CheckpointStore(str(workdir)).load("v1", "svc-1")
    assert state is not None
    assert len(state["completed"]) == 3

    service = samplegen.make_service()
    report = run_pipeline(samples["decl"], samples["manifest"], service,
                          EngineConfig(workers=2), str(workdir))
    assert report["counts"]["total"] == 8
    assert len(service.query_log()) == 5

    clean, _, _ = _run(samples, tmp_path, name="clean",
                       config=EngineConfig(workers=2))
    assert _projection(report) == _projection(clean)


def test_version_change_triggers_rebuild(samples, tmp_path):
    workdir = tmp_path / "versioned"
    run_pipeline(samples["decl"], samples["manifest"],
                 samplegen.make_service(), EngineConfig(), str(workdir))
    store = CheckpointStore(str(workdir))
    assert store.load("v1", "svc-1") is not None
    assert store.load("v2", "svc-1") is None
    assert store.load("v1", "svc-2") is None

    decl_v2 = dict(samples["decl"], policy_version="v2")
    service = samplegen.make_service()
    service.load_declaration(decl_v2)
    report = run_pipeline(decl_v2, samples["manifest"], service,
                          EngineConfig(), str(workdir))
    assert report["policy_version"] == "v2"
    assert report["counts"]["total"] == 8
    assert len(service.query_log()) == 8


def test_lock_conflict_returns_e_lock(samples, tmp_path):
    store = CheckpointStore(str(tmp_path))
    with FileLock(store.path):
        with pytest.raises(LockError) as excinfo:
            store.save("v1", "svc-1", {}, [])
    assert excinfo.value.code == errors.E_LOCK


def test_report_file_written_atomically_and_sorted(samples, tmp_path):
    report, _, workdir = _run(samples, tmp_path)
    on_disk = json.loads((workdir / "report.json").read_text(encoding="utf-8"))
    assert on_disk["report_version"] == 1
    assert [r["request_id"] for r in on_disk["requests"]] == [
        "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8"]
    assert on_disk["counts"] == report["counts"]
    leftovers = [p for p in os.listdir(str(workdir))
                 if p.endswith(".lock") or ".tmp." in p]
    assert leftovers == []
