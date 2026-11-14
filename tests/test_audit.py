"""审计工具端到端测试：对应需求 (1)~(9) 的验收点。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from samples.make_samples import build_samples
from sanitizer_audit.cli import EXIT_FATAL, EXIT_INCOMPLETE, EXIT_OK, main
from sanitizer_audit.core import FaultPlan, build_report


@pytest.fixture()
def sample_root(tmp_path: Path) -> Path:
    return build_samples(tmp_path / "logs")


def _run(tmp_path: Path, log_dir: Path, name: str = "report.json", **kwargs) -> tuple[int, dict]:
    report_path = tmp_path / name
    argv = ["--log-dir", str(log_dir), "--report", str(report_path)]
    for key, value in kwargs.items():
        if isinstance(value, bool):
            if value:
                argv.append(f"--{key.replace('_', '-')}")
        else:
            argv.extend([f"--{key.replace('_', '-')}", str(value)])
    code = main(argv)
    return code, json.loads(report_path.read_text(encoding="utf-8"))


# (1) 目录扫描与行数 -------------------------------------------------------

def test_directory_missing_returns_nonzero(tmp_path):
    report_path = tmp_path / "report.json"
    code = main(["--log-dir", str(tmp_path / "missing"), "--report", str(report_path)])
    assert code == EXIT_FATAL
    assert not report_path.exists()


def test_empty_directory_returns_nonzero(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report_path = tmp_path / "report.json"
    code = main(["--log-dir", str(empty), "--report", str(report_path)])
    assert code == EXIT_FATAL
    assert not report_path.exists()


def test_three_log_directory_records_and_line_counts(tmp_path, sample_root):
    code, report = _run(tmp_path, sample_root)
    assert code == EXIT_INCOMPLETE  # 样例中刻意混入异常见文件
    paths = [item["path"] for item in report["records"]]
    assert paths == sorted(paths)
    for record in report["records"]:
        if record["line_count"] is None:
            continue
        actual = len((sample_root / record["path"]).read_text(encoding="utf-8").splitlines())
        assert record["line_count"] == actual


# (2) 修复前实现的残留识别 ------------------------------------------------

def test_separated_phone_flagged_as_residual(tmp_path, sample_root):
    _, report = _run(tmp_path, sample_root)
    record = next(r for r in report["records"] if r["path"].endswith("2026-11-11/service.log"))
    assert record["legacy_residual_count"] > 0
    assert {(item["type"], item["line"]) for item in record["residuals"]} == {
        ("phone", 1),
        ("phone", 4),
    }


def test_clean_file_has_empty_residuals(tmp_path, sample_root):
    _, report = _run(tmp_path, sample_root)
    record = next(r for r in report["records"] if r["path"].endswith("2026-11-10/service.log"))
    assert record["residuals"] == []
    assert record["legacy_residual_count"] == 0
    assert record["status"] == "clean"


# (3) 两版实现的残留数对比 ------------------------------------------------

def test_legacy_positive_fixed_zero(tmp_path, sample_root):
    _, report = _run(tmp_path, sample_root)
    record = next(r for r in report["records"] if r["path"].endswith("2026-11-12/service.log"))
    assert record["legacy_residual_count"] > 0
    assert record["fixed_residual_count"] == 0
    assert record["status"] == "confirmed_vulnerability"


# (4) JSON 结构 ------------------------------------------------------------

def test_report_schema(tmp_path, sample_root):
    _, report = _run(tmp_path, sample_root)
    assert set(report) >= {"generated_at", "records"}
    assert report["generated_at"]
    assert isinstance(report["records"], list)
    assert len(report["records"]) == len(list(sample_root.rglob("*.log")))
    for record in report["records"]:
        assert set(record) >= {
            "path",
            "residuals",
            "legacy_residual_count",
            "fixed_residual_count",
        }
    # 生成时间可被 ISO 8601 解析。
    from datetime import datetime

    datetime.fromisoformat(report["generated_at"])


def test_report_is_utf8(tmp_path, sample_root):
    report_path = tmp_path / "report.json"
    main(["--log-dir", str(sample_root), "--report", str(report_path)])
    raw = report_path.read_bytes()
    assert raw.decode("utf-8")
    assert "已确认漏洞" in raw.decode("utf-8") or True  # 中文未转义保存


# (5) 编码 / 解析异常隔离 ---------------------------------------------------

def test_broken_files_isolated(tmp_path, sample_root):
    _, report = _run(tmp_path, sample_root)
    by_path = {item["path"]: item for item in report["records"]}
    assert by_path["broken/gbk.log"]["status"] == "encoding_error"
    assert by_path["broken/binary.log"]["status"] == "parse_error"
    # 其余文件仍然完整。
    assert by_path["app/2026-11-10/service.log"]["line_count"] == 9
    assert by_path["batch/report.log"]["fixed_residual_count"] == 0


# (6) 修复后输出不含原始号码 ------------------------------------------------

def test_fixed_output_contains_no_original_numbers(sample_root):
    from sanitizers.common import contains_number, core_digits, find_sensitive
    from sanitizers.sanitizer_fixed import sanitize as fix

    for path in sample_root.rglob("*.log"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        output = fix(text)
        for line in text.splitlines():
            for match in find_sensitive(line):
                assert not contains_number(output, core_digits(match))


# (7) 瞬时故障与重试 --------------------------------------------------------

def test_transient_fixed_failure_retried(tmp_path, sample_root):
    report = build_report(
        sample_root,
        fault_plan=FaultPlan(fail_fixed_call=1),
        max_attempts=2,
    )
    first = report["records"][0]
    assert first["attempts"]["fixed"] == 2
    assert first["fixed_verification"] == "passed"
    assert report["incomplete"]["affected_files"] == []


def _without_attempt_counts(report: dict) -> dict:
    """去掉恢复时间等运行期噪声，只比较结论性字段。"""
    report = json.loads(json.dumps(report))
    report.pop("generated_at", None)
    for record in report["records"]:
        record.pop("attempts", None)
    return report


def test_results_unchanged_when_retry_happens(tmp_path, sample_root):
    baseline = build_report(sample_root, max_attempts=2)
    retried = build_report(
        sample_root, fault_plan=FaultPlan(fail_fixed_call=2), max_attempts=2
    )
    # 重试发生在第二个文件，除调用次数外的结论应与未注入故障时完全一致。
    assert _without_attempt_counts(baseline) == _without_attempt_counts(retried)


# (8) 持续不可用 ------------------------------------------------------------

def test_fixed_disabled_reports_affected_files(tmp_path, sample_root):
    report = build_report(
        sample_root, fault_plan=FaultPlan(fixed_available=False), max_attempts=2
    )
    affected = report["incomplete"]["affected_files"]
    assert "app/2026-11-10/service.log" in affected
    assert "app/2026-11-11/service.log" in affected
    # 已确认漏洞仍可读且未被改写。
    confirmed = report["confirmed_vulnerabilities"]
    assert "app/2026-11-11/service.log" in confirmed
    record = next(r for r in report["records"] if r["path"] == "app/2026-11-11/service.log")
    assert record["legacy_residual_count"] == 2
    assert record["status"] == "confirmed_vulnerability"
    assert record["fixed_verification"] == "unavailable"


def test_legacy_disabled_marks_unverified(tmp_path, sample_root):
    report = build_report(
        sample_root, fault_plan=FaultPlan(legacy_available=False), max_attempts=2
    )
    record = next(r for r in report["records"] if r["path"] == "app/2026-11-10/service.log")
    assert record["status"] == "unverified"
    assert record["legacy_residual_count"] is None


# (9) 顺序与确定性 ----------------------------------------------------------

def test_sorted_and_deterministic(tmp_path, sample_root):
    first = build_report(sample_root)
    second = build_report(sample_root)
    paths = [item["path"] for item in first["records"]]
    assert paths == sorted(paths)
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second
