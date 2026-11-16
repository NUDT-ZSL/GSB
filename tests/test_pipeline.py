"""并发一致性、快照恢复、注入可复现性、失败隔离与报告链路。"""
import json

import pytest

from qu_service.errors import ForcedTermination
from qu_service.report import strip_volatile
from qu_service.snapshot import snapshot_path

from pathlib import Path

INJ = str(Path(__file__).resolve().parent.parent / "data" / "injection.json")


def _business_view(report):
    view = strip_volatile(report)
    view.pop("run_name", None)
    view.pop("config_hash", None)
    return view


def test_serial_parallel_identical(pipeline_factory, tmp_path):
    serial = pipeline_factory(run_name="s", max_workers=1,
                              snapshot_dir=str(tmp_path / "s"),
                              report_path=str(tmp_path / "s.json")).run()
    parallel = pipeline_factory(run_name="p", max_workers=8,
                                snapshot_dir=str(tmp_path / "p"),
                                report_path=str(tmp_path / "p.json")).run()
    assert _business_view(serial) == _business_view(parallel)


def test_many_concurrency_levels_identical(pipeline_factory, tmp_path):
    baseline = None
    for idx, workers in enumerate((1, 2, 3, 5, 16)):
        report = pipeline_factory(
            run_name=f"w{workers}", max_workers=workers,
            snapshot_dir=str(tmp_path / f"w{workers}"),
            report_path=str(tmp_path / f"w{workers}.json")).run()
        view = _business_view(report)
        if baseline is None:
            baseline = view
        assert view == baseline


def test_empty_query_failure_isolated(pipeline_factory):
    report = pipeline_factory().run()
    failures = {f["query_id"]: f["code"] for f in report["failures"]}
    assert failures["Q008"] == "E_EMPTY_QUERY"
    assert failures["Q009"] == "E_EMPTY_QUERY"
    # 其余样本不受影响
    assert report["processed"] == report["total_queries"] - 2


def test_crash_then_resume_identical(pipeline_factory, tmp_path):
    from qu_service.pipeline import Pipeline
    crashed = pipeline_factory(stop_after_stage="intent")
    with pytest.raises(ForcedTermination):
        crashed.run()
    # 崩溃后快照已持久化到 intent 阶段
    assert snapshot_path(tmp_path / "snapshots", "intent").exists()
    assert not snapshot_path(tmp_path / "snapshots", "slots").exists()

    # 同一配置从快照恢复（stop_after_stage 在恢复时忽略）
    resumed = Pipeline(crashed.config_path).run(resume=True)
    fresh = pipeline_factory(run_name="fresh",
                             snapshot_dir=str(tmp_path / "fresh_snap"),
                             report_path=str(tmp_path / "fresh.json")).run()
    assert _business_view(resumed) == _business_view(fresh)


def test_resume_config_mismatch_refused(pipeline_factory, tmp_path):
    from qu_service.errors import ConfigMismatch
    from qu_service.pipeline import Pipeline
    pipe = pipeline_factory()
    pipe.run()
    # 篡改配置（改变 max_rewrites）后恢复 -> E_CONFIG_MISMATCH
    cfg = json.loads(pipe.config_path.read_text(encoding="utf-8"))
    cfg["max_rewrites"] = 3
    pipe.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ConfigMismatch):
        Pipeline(pipe.config_path).run(resume=True)


def test_injection_reproducible(pipeline_factory, tmp_path):
    first = pipeline_factory(injection=INJ, run_name="i1",
                             snapshot_dir=str(tmp_path / "i1"),
                             report_path=str(tmp_path / "i1.json")).run()
    second = pipeline_factory(injection=INJ, run_name="i2",
                              snapshot_dir=str(tmp_path / "i2"),
                              report_path=str(tmp_path / "i2.json")).run()
    assert _business_view(first) == _business_view(second)
    # 注入配置哈希纳入报告
    assert first["injection_hash"]
    # 失败清单与业务码序列确定
    codes = [f["code"] for f in first["failures"]]
    assert codes == [f["code"] for f in second["failures"]]
    assert "E_MALFORMED_QUERY" in codes


def test_injection_hit_matrix_and_codes(pipeline_factory):
    report = pipeline_factory(injection=INJ).run()
    matrix = report["injection_hit_matrix"]
    assert "INJ_MALFORMED" in matrix and matrix["INJ_MALFORMED"]
    assert "INJ_DICT_REMOVE:手机壳" in matrix
    assert any(k.startswith("INJ_RULE_CONFLICT:") for k in matrix)
    assert any(k.startswith("INJ_SCORE_OOR:") for k in matrix)
    summary = report["codes_summary"]
    for code in ("E_MALFORMED_QUERY", "E_RULE_MISSING", "E_RULE_CONFLICT",
                 "E_SCORE_OUT_OF_RANGE"):
        assert code in summary
    # 词典条目移除导致规则引用缺失
    assert report["rule_missing"][0]["code"] == "E_RULE_MISSING"


def test_report_atomic_and_complete(pipeline_factory, tmp_path):
    report = pipeline_factory().run()
    path = tmp_path / "report.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    for key in ("intent_distribution", "slot_stats", "rewrite_stats",
                "injection_hit_matrix", "failures", "stage_timings",
                "report_fingerprint", "config_hash", "injection_hash"):
        assert key in loaded
    assert loaded["report_fingerprint"] == report["report_fingerprint"]
    # 无临时文件残留
    assert not list(tmp_path.glob(".*.tmp"))


def test_stable_sort_keys_present(pipeline_factory):
    report = pipeline_factory(injection=INJ).run()
    for failure in report["failures"]:
        assert len(failure["sort_key"]) == 64  # sha256 hex
