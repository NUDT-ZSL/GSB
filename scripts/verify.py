#!/usr/bin/env python3
"""Run the local acceptance matrix and emit an atomic JSON/Markdown validation report."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ctr_pipeline.io_utils import file_digest, write_json_atomic  # noqa: E402
from scripts.generate_samples import build_samples  # noqa: E402


SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts"
RUNS = ARTIFACTS / "verification"


def _run_cli(
    *,
    config: Path,
    state: Path,
    report: Path,
    concurrency: int,
    extra: list[str] | None = None,
    params: Path | None = None,
    timeout: int = 90,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "ctr_pipeline.cli",
        "run",
        "--config",
        str(config),
        "--candidates",
        str(SAMPLES / "candidates.jsonl"),
        "--params",
        str(params or (SAMPLES / "params.json")),
        "--state-dir",
        str(state),
        "--report",
        str(report),
        "--concurrency",
        str(concurrency),
    ]
    if extra:
        command.extend(extra)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "report_path": str(report),
        "state_dir": str(state),
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _artifact_versions(state_dir: Path) -> dict[str, Any]:
    manifest = _read_json(state_dir / "manifest.json")
    return {
        request_id: record.get("artifact_versions", {})
        for request_id, record in sorted(manifest["request_records"].items())
    }


def _check(check_id: str, description: str, condition: bool, details: Any = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "description": description,
        "passed": bool(condition),
        "details": details,
    }


def _width_field_comparison(serial: dict[str, Any], parallel: dict[str, Any]) -> dict[str, Any]:
    serial_requests = {item["request_id"]: item for item in serial["requests"]}
    parallel_requests = {item["request_id"]: item for item in parallel["requests"]}
    width_results: dict[str, Any] = {}
    for width_label in ("configured", "high"):
        mismatches: list[str] = []
        for request_id in sorted(serial_requests):
            left = serial_requests[request_id]["width_results"][width_label]
            right = parallel_requests[request_id]["width_results"][width_label]
            for field in (
                "width",
                "scale",
                "candidates",
                "ranking",
                "score_fingerprint",
                "stage_fingerprints",
                "normalization_stats",
            ):
                if left.get(field) != right.get(field):
                    mismatches.append(f"{request_id}.{field}")
        width_results[width_label] = {
            "request_count": len(serial_requests),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
        }
    return width_results


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# CTR 精排管线验证报告",
        "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- Python：`{report['environment']['python']}`",
        f"- numpy：`{report['environment']['numpy']}`",
        f"- PyYAML：`{report['environment']['pyyaml']}`",
        f"- 总体结果：`{'PASS' if report['all_passed'] else 'FAIL'}`",
        "",
        "## 验收检查",
        "",
        "| 检查项 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for check in report["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        details = check.get("details")
        detail_text = ""
        if isinstance(details, (str, int, float, bool)) or details is None:
            detail_text = "" if details is None else str(details)
        else:
            detail_text = json.dumps(details, ensure_ascii=False, sort_keys=True)
        if len(detail_text) > 180:
            detail_text = detail_text[:177] + "..."
        detail_text = detail_text.replace("|", "\\|")
        lines.append(f"| `{check['id']}` | **{status}** | {check['description']} {detail_text} |")
    lines.extend(
        [
            "",
            "## 关键结果",
            "",
            f"- 串行一致性摘要：`{report['runs']['serial']['determinism_digest']}`",
            f"- 并发一致性摘要：`{report['runs']['parallel']['determinism_digest']}`",
            f"- 快照版本：`{report['runs']['parallel']['snapshot_version']}`",
            f"- 成功请求数：`{report['runs']['parallel']['request_count']}`",
            f"- 失败请求数：`{report['runs']['parallel']['failure_count']}`",
            f"- 预算消耗 / 总预算：`{report['runs']['parallel']['budget_consumed']} / "
            f"{report['runs']['parallel']['budget_global']}`",
            f"- 边界敏感样本数：`{report['runs']['parallel']['boundary_sensitive_sample_count']}`",
            f"- 业务码集合：`{', '.join(report['observed_business_codes'])}`",
            "",
            "## 复现命令",
            "",
            "```bash",
            "python3 scripts/generate_samples.py",
            "python3 scripts/verify.py",
            "python3 -m unittest -v tests.test_pipeline",
            "```",
            "",
            "验证报告由 `scripts/verify.py` 以临时文件加 `os.replace` 原子写入。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _clean(RUNS)
    RUNS.mkdir(parents=True, exist_ok=True)
    _clean(ARTIFACTS / "validation_report.json")
    _clean(ROOT / "VALIDATION.md")
    build_samples(SAMPLES)

    runs: dict[str, Any] = {}
    reports: dict[str, dict[str, Any]] = {}

    serial = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "serial_state",
        report=RUNS / "serial_report.json",
        concurrency=1,
    )
    runs["serial"] = serial
    reports["serial"] = _read_json(RUNS / "serial_report.json")

    parallel = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "parallel_state",
        report=RUNS / "parallel_report.json",
        concurrency=4,
    )
    runs["parallel"] = parallel
    reports["parallel"] = _read_json(RUNS / "parallel_report.json")

    graceful = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "graceful_state",
        report=RUNS / "graceful_partial.json",
        concurrency=4,
        extra=["--exit-after", "3"],
    )
    runs["graceful_partial"] = graceful
    graceful_partial = _read_json(RUNS / "graceful_partial.json")
    graceful_resume = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "graceful_state",
        report=RUNS / "graceful_resumed.json",
        concurrency=4,
    )
    runs["graceful_resume"] = graceful_resume
    reports["graceful"] = _read_json(RUNS / "graceful_resumed.json")

    crash = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "crash_state",
        report=RUNS / "crash_partial.json",
        concurrency=4,
        extra=["--crash-after", "3"],
    )
    runs["crash"] = crash
    crash_manifest = _read_json(RUNS / "crash_state" / "manifest.json")
    crash_resume = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "crash_state",
        report=RUNS / "crash_resumed.json",
        concurrency=4,
    )
    runs["crash_resume"] = crash_resume
    reports["crash"] = _read_json(RUNS / "crash_resumed.json")

    raw_config = yaml.safe_load((SAMPLES / "config.yaml").read_text(encoding="utf-8"))
    raw_config["fusion"]["temperature"] = 0.71
    mismatch_config = RUNS / "mismatch_config.yaml"
    mismatch_config.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    mismatch = _run_cli(
        config=mismatch_config,
        state=RUNS / "parallel_state",
        report=RUNS / "mismatch_report.json",
        concurrency=1,
    )
    runs["config_mismatch"] = mismatch
    mismatch_report = _read_json(RUNS / "mismatch_report.json")

    raw_params = json.loads((SAMPLES / "params.json").read_text(encoding="utf-8"))
    raw_params["feature_groups"]["unregistered_group"] = [0]
    unknown_params = RUNS / "unknown_group_params.json"
    unknown_params.write_text(
        json.dumps(raw_params, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    unknown_group = _run_cli(
        config=SAMPLES / "config.yaml",
        state=RUNS / "unknown_group_state",
        report=RUNS / "unknown_group_report.json",
        concurrency=1,
        params=unknown_params,
    )
    runs["unknown_group"] = unknown_group
    unknown_report = _read_json(RUNS / "unknown_group_report.json")

    raw_width_config = yaml.safe_load((SAMPLES / "config.yaml").read_text(encoding="utf-8"))
    raw_width_config["fixed_point"]["width"] = 99
    width_config = RUNS / "invalid_width_config.yaml"
    width_config.write_text(
        yaml.safe_dump(raw_width_config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    width_invalid = _run_cli(
        config=width_config,
        state=RUNS / "width_invalid_state",
        report=RUNS / "width_invalid_report.json",
        concurrency=1,
    )
    runs["width_invalid"] = width_invalid
    width_report = _read_json(RUNS / "width_invalid_report.json")

    serial_report = reports["serial"]
    parallel_report = reports["parallel"]
    width_comparison = _width_field_comparison(serial_report, parallel_report)
    serial_artifacts = _artifact_versions(RUNS / "serial_state")
    parallel_artifacts = _artifact_versions(RUNS / "parallel_state")
    graceful_artifacts = _artifact_versions(RUNS / "graceful_state")
    crash_artifacts = _artifact_versions(RUNS / "crash_state")

    observed_codes = sorted(
        {
            item["code"]
            for report in (
                parallel_report,
                graceful_partial,
                crash_manifest,
                mismatch_report,
                unknown_report,
                width_report,
            )
            for item in (
                report.get("failure_list", [])
                if isinstance(report, dict)
                else []
            )
        }
        | {
            item["code"]
            for item in graceful_partial.get("failure_list", [])
        }
    )
    # Include crash manifest failure records if any were already committed.
    if isinstance(crash_manifest, dict):
        observed_codes = sorted(
            set(observed_codes)
            | {item["code"] for item in crash_manifest.get("failure_list", [])}
        )

    checks = [
        _check(
            "serial_parallel_digest",
            "串行与并发报告逐字段一致性摘要相同。",
            serial_report["determinism_digest"] == parallel_report["determinism_digest"],
            serial_report["determinism_digest"],
        ),
        _check(
            "configured_width_fields",
            "配置位宽下候选、排序、贡献分解和阶段指纹逐字段一致。",
            width_comparison["configured"]["mismatch_count"] == 0,
            width_comparison["configured"],
        ),
        _check(
            "high_width_fields",
            "高一档位宽下候选、排序、贡献分解和阶段指纹逐字段一致。",
            width_comparison["high"]["mismatch_count"] == 0,
            width_comparison["high"],
        ),
        _check(
            "serial_parallel_artifacts",
            "串行与并发的四阶段快照产物版本一致。",
            serial_artifacts == parallel_artifacts,
            {"request_count": len(serial_artifacts)},
        ),
        _check(
            "budget_exhaustion",
            "全局预算精确耗尽并记录预算终止失败。",
            parallel_report["budget"]["consumed"] == parallel_report["budget"]["global_budget"]
            and parallel_report["budget"]["remaining"] == 0
            and any(item["code"] == "E_BUDGET_EXHAUSTED" for item in parallel_report["failure_list"]),
            {
                "consumed": parallel_report["budget"]["consumed"],
                "global_budget": parallel_report["budget"]["global_budget"],
                "remaining": parallel_report["budget"]["remaining"],
            },
        ),
        _check(
            "quota_exhaustion",
            "单请求配额超限被隔离并以 E_REQ_QUOTA_EXCEEDED 记录。",
            any(item["code"] == "E_REQ_QUOTA_EXCEEDED" for item in parallel_report["failure_list"]),
            next(
                item
                for item in parallel_report["failure_list"]
                if item["code"] == "E_REQ_QUOTA_EXCEEDED"
            ),
        ),
        _check(
            "injection_hits",
            "三类异常注入全部命中并进入失败清单。",
            len(parallel_report["injections"]) == 3
            and all(item["hit"] for item in parallel_report["injections"])
            and {"E_NON_FINITE", "E_WEIGHT_SHAPE", "E_WIDTH_INVALID"}
            <= {item["code"] for item in parallel_report["failure_list"]},
            parallel_report["injections"],
        ),
        _check(
            "boundary_sensitive",
            "高位宽排序翻转样本及其贡献差异被独立记录。",
            parallel_report["error_trace_summary"]["boundary_sensitive_sample_count"] >= 1
            and all(
                item["error_trace"]["boundary_sensitive_samples"][0]["candidate_pair"]
                and item["error_trace"]["boundary_sensitive_samples"][0]["flip_contributing_group"]
                for item in parallel_report["requests"]
                if item["error_trace"]["boundary_sensitive_samples"]
            ),
            parallel_report["error_trace_summary"],
        ),
        _check(
            "graceful_exit",
            "可处理退出在提交快照后以 0 退出，并从游标续跑。",
            graceful["returncode"] == 0
            and graceful_partial["status"] == "interrupted"
            and graceful_partial["snapshot_version"] == 3
            and graceful_resume["returncode"] == 0
            and reports["graceful"]["determinism_digest"] == parallel_report["determinism_digest"],
            {
                "partial_status": graceful_partial["status"],
                "partial_snapshot_version": graceful_partial["snapshot_version"],
            },
        ),
        _check(
            "forced_termination_resume",
            "强制终止后从最后原子清单恢复且结果一致。",
            crash["returncode"] == 137
            and crash_manifest["global_version"] == 3
            and crash_manifest["cursor_index"] == 3
            and crash_resume["returncode"] == 0
            and reports["crash"]["determinism_digest"] == parallel_report["determinism_digest"]
            and crash_artifacts == parallel_artifacts,
            {
                "crash_returncode": crash["returncode"],
                "manifest_version": crash_manifest["global_version"],
                "cursor": crash_manifest["cursor_index"],
            },
        ),
        _check(
            "config_mismatch_rejected",
            "配置哈希变化时拒绝续跑并输出 E_CONFIG_MISMATCH。",
            mismatch["returncode"] == 2
            and mismatch_report["failure_list"][0]["code"] == "E_CONFIG_MISMATCH",
            mismatch_report["failure_list"][0],
        ),
        _check(
            "unknown_group_code",
            "未知特征组输出 E_GROUP_UNKNOWN。",
            unknown_group["returncode"] == 3
            and unknown_report["failure_list"][0]["code"] == "E_GROUP_UNKNOWN",
            unknown_report["failure_list"][0],
        ),
        _check(
            "width_invalid_code",
            "配置位宽越界输出 E_WIDTH_INVALID。",
            width_invalid["returncode"] == 3
            and width_report["failure_list"][0]["code"] == "E_WIDTH_INVALID",
            width_report["failure_list"][0],
        ),
        _check(
            "global_loader_codes",
            "主样本包含 E_REQ_EMPTY 与 E_JSON_INVALID 失败隔离。",
            {"E_REQ_EMPTY", "E_JSON_INVALID"}
            <= {item["code"] for item in parallel_report["failure_list"]},
            sorted({item["code"] for item in parallel_report["failure_list"]}),
        ),
        _check(
            "atomic_no_temp_files",
            "运行目录不残留原子写临时文件。",
            not any(RUNS.rglob("*.tmp")),
            {"tmp_count": len(list(RUNS.rglob("*.tmp")))},
        ),
    ]

    all_passed = all(item["passed"] for item in checks) and all(
        runs[name]["returncode"] == expected
        for name, expected in (
            ("serial", 0),
            ("parallel", 0),
            ("graceful_partial", 0),
            ("graceful_resume", 0),
            ("crash", 137),
            ("crash_resume", 0),
            ("config_mismatch", 2),
            ("unknown_group", 3),
            ("width_invalid", 3),
        )
    )

    validation_report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pyyaml": yaml.__version__,
            "workspace_writable": os.access(ROOT, os.W_OK),
        },
        "sample_hashes": {
            name: file_digest(path)
            for name, path in {
                "candidates": SAMPLES / "candidates.jsonl",
                "params": SAMPLES / "params.json",
                "config": SAMPLES / "config.yaml",
                "injections": SAMPLES / "injections.json",
            }.items()
        },
        "runs": {
            "serial": {
                "returncode": serial["returncode"],
                "status": serial_report["status"],
                "determinism_digest": serial_report["determinism_digest"],
                "snapshot_version": serial_report["snapshot_version"],
                "request_count": serial_report["request_count"],
                "failure_count": serial_report["failure_count"],
            },
            "parallel": {
                "returncode": parallel["returncode"],
                "status": parallel_report["status"],
                "determinism_digest": parallel_report["determinism_digest"],
                "snapshot_version": parallel_report["snapshot_version"],
                "request_count": parallel_report["request_count"],
                "failure_count": parallel_report["failure_count"],
                "budget_consumed": parallel_report["budget"]["consumed"],
                "budget_global": parallel_report["budget"]["global_budget"],
                "boundary_sensitive_sample_count": parallel_report["error_trace_summary"][
                    "boundary_sensitive_sample_count"
                ],
                "rankings": {
                    item["request_id"]: item["ranking"] for item in parallel_report["requests"]
                },
            },
            "graceful": {
                "partial_returncode": graceful["returncode"],
                "partial_status": graceful_partial["status"],
                "partial_snapshot_version": graceful_partial["snapshot_version"],
                "resume_returncode": graceful_resume["returncode"],
                "final_status": reports["graceful"]["status"],
                "determinism_digest": reports["graceful"]["determinism_digest"],
            },
            "crash": {
                "returncode": crash["returncode"],
                "manifest_version": crash_manifest["global_version"],
                "cursor": crash_manifest["cursor_index"],
                "resume_returncode": crash_resume["returncode"],
                "final_status": reports["crash"]["status"],
                "determinism_digest": reports["crash"]["determinism_digest"],
            },
            "config_mismatch": {
                "returncode": mismatch["returncode"],
                "status": mismatch_report["status"],
                "code": mismatch_report["failure_list"][0]["code"],
            },
            "unknown_group": {
                "returncode": unknown_group["returncode"],
                "code": unknown_report["failure_list"][0]["code"],
            },
            "width_invalid": {
                "returncode": width_invalid["returncode"],
                "code": width_report["failure_list"][0]["code"],
            },
        },
        "width_comparison": width_comparison,
        "observed_business_codes": observed_codes,
        "checks": checks,
        "all_passed": all_passed,
    }

    report_path = ARTIFACTS / "validation_report.json"
    write_json_atomic(report_path, validation_report)
    markdown_path = ROOT / "VALIDATION.md"
    # Use a same-directory temporary file and replace for the Markdown artifact too.
    temp_markdown = markdown_path.with_name(f".{markdown_path.name}.tmp")
    temp_markdown.write_text(_build_markdown(validation_report), encoding="utf-8")
    os.replace(temp_markdown, markdown_path)

    print(json.dumps({"all_passed": all_passed, "report": str(report_path)}, ensure_ascii=False))
    for check in checks:
        print(f"[{'PASS' if check['passed'] else 'FAIL'}] {check['id']}: {check['description']}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
