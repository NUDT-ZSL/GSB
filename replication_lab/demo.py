"""Deterministic three-scenario demonstration for the local workbench."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import StateError
from .reports import render_observation_report, render_verification_report
from .segment import LogRecord, build_segment_bytes
from .service import LabSession, load_operations_file
from .util import atomic_write_bytes, atomic_write_json, ensure_dir, mark_read_only, stable_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_OPERATIONS = PROJECT_ROOT / "samples" / "deterministic_operations.json"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise StateError("demo assertion failed: " + message)


def _fresh(path: Path) -> Path:
    if path.exists():
        def make_writable_and_retry(function: Any, target: str, _info: Any) -> None:
            os.chmod(target, 0o666)
            function(target)

        shutil.rmtree(path, onerror=make_writable_and_retry)
    return ensure_dir(path)


def _scenario_summary(name: str, report: dict[str, Any], **extra: Any) -> dict[str, Any]:
    estimate = report["estimation"]
    return {
        "scenario": name,
        "ok": True,
        "format_version_set": report["format_version_set"],
        "sample_count": report["sample_count"],
        "known_delay_count": report["known_delay_count"],
        "unknown_delay_count": report["unknown_delay_count"],
        "estimate_status": report["estimate_status"],
        "confidence": report["confidence"],
        "mean_fixed": estimate["mean_fixed"],
        "mean_fixed_scale": estimate["mean_fixed_scale"],
        "p95_ms": estimate["p95_ms"],
        "max_delay_ms": estimate["max_delay_ms"],
        "conclusion": report["conclusion"],
        "observation_report": report,
        **extra,
    }


def _write_scenario_artifacts(
    scenario_dir: Path,
    scenario: dict[str, Any],
) -> None:
    ensure_dir(scenario_dir)
    atomic_write_json(scenario_dir / "scenario.json", scenario)
    atomic_write_bytes(
        scenario_dir / "observation.md",
        render_observation_report(scenario["observation_report"]).encode("utf-8"),
    )


def _prepare_mixed_logs(root: Path, operations: list[dict[str, Any]]) -> None:
    logs = ensure_dir(root / "logs")
    specs = [
        (1, 1, operations[0:2]),
        (2, 2, operations[2:5]),
        (3, 3, operations[5:8]),
    ]
    for segment_id, version, chunk in specs:
        records: list[LogRecord] = []
        for operation in chunk:
            records.append(
                LogRecord(
                    format_version=version,
                    commit_seq=operation["commit_seq"],
                    txn_id=operation["txn_id"],
                    commit_ts_ms=(
                        None
                        if version == 1
                        else 1_000_000 + operation["commit_offset_ms"]
                    ),
                    txn_size_bytes=None if version == 1 else operation["txn_size_bytes"],
                    segment_id=segment_id if version == 3 else None,
                    segment_offset=None,
                )
            )
        data = build_segment_bytes(
            records,
            segment_id=segment_id,
            target_version=version,
            filename=f"seg-{segment_id:08d}.rlog",
        )
        atomic_write_bytes(logs / f"seg-{segment_id:08d}.rlog", data)


def _scenario_cold_start(base: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    scenario_dir = _fresh(base / "scenario1_cold_start")
    with LabSession.open(scenario_dir) as session:
        write_result = session.write_operations(operations[0:8], records_per_segment=4)
        replay = session.replay(replay_rate=10, concurrency=2, reset=True)
        report = session.observation_report()
        snapshot = session.save_snapshot()
        verification = session.verify_snapshot(snapshot["snapshot_id"])
        _assert(report["sample_count"] == 8, "cold start sample count")
        _assert(report["known_delay_count"] == 8, "cold start known delays")
        _assert(report["estimate_status"] == "COMPLETE", "cold start estimate status")
        _assert(verification["ok"], "cold start snapshot verification")
        scenario = _scenario_summary(
            "冷启动回放观测",
            report,
            written_segment_count=len(write_result["segments"]),
            replay_parameters=replay["replay_parameters"],
            snapshot_id=snapshot["snapshot_id"],
            verification_ok=verification["ok"],
            verification_business_code=verification["business_code"],
        )
    _write_scenario_artifacts(scenario_dir, scenario)
    return scenario


def _scenario_upgrade(base: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    scenario_dir = _fresh(base / "scenario2_upgrade_observe")
    _prepare_mixed_logs(scenario_dir, operations)

    # Save a legacy-format snapshot before migration to make snapshot-to-snapshot
    # version differences observable and stable.
    with LabSession.open(scenario_dir, auto_upgrade=False) as legacy_session:
        legacy_versions = legacy_session.version_set()
        legacy_snapshot = legacy_session.save_snapshot()
        legacy_snapshot_id = legacy_snapshot["snapshot_id"]
        _assert(legacy_versions == [1, 2, 3], "legacy version set")

    with LabSession.open(scenario_dir, auto_upgrade=True) as session:
        upgrade_report = session.upgrade_report or {}
        _assert(upgrade_report.get("version_set_before") == [1, 2, 3], "upgrade before set")
        _assert(upgrade_report.get("version_set_after") == [3], "upgrade after set")
        _assert(not upgrade_report.get("failed"), "upgrade failures")
        after_keys = {path.name for path in (scenario_dir / "logs").glob("*.rlog")}
        _assert(len(after_keys) == 3, "upgraded segment count")
        backup_files = sorted((scenario_dir / "backups").glob("*.bak"))
        _assert(len(backup_files) == 2, "backup count for the two legacy segments")
        # The v1 source has no timestamps, so those delays must remain unknown even
        # after the records are rewritten as current-version v3 records.
        session.write_operations(operations[8:10], records_per_segment=2)
        session.replay(replay_rate=10, concurrency=2, reset=True)
        report = session.observation_report()
        post_snapshot = session.save_snapshot()
        verification = session.verify_snapshot(post_snapshot["snapshot_id"])
        diff = session.snapshot_diff(legacy_snapshot_id, post_snapshot["snapshot_id"])
        _assert(report["format_version_set"] == [3], "upgraded version set")
        _assert(report["sample_count"] == 10, "upgraded sample count")
        _assert(report["known_delay_count"] == 8, "upgraded known count")
        _assert(report["unknown_delay_count"] == 2, "upgraded unknown count")
        _assert(report["estimate_status"] == "DEGRADED_PARTIAL", "upgraded estimate status")
        _assert(verification["ok"], "upgraded snapshot verification")
        _assert(diff["version_diff"]["changed"], "snapshot version diff")
        scenario = _scenario_summary(
            "格式升级后重新观测",
            report,
            legacy_format_version_set=legacy_versions,
            upgraded_format_version_set=session.version_set(),
            backup_files=[path.name for path in backup_files],
            upgrade_report=upgrade_report,
            legacy_snapshot_id=legacy_snapshot_id,
            post_snapshot_id=post_snapshot["snapshot_id"],
            snapshot_diff=diff,
            verification_ok=verification["ok"],
            verification_business_code=verification["business_code"],
        )
    _write_scenario_artifacts(scenario_dir, scenario)
    return scenario


def _scenario_crash(base: Path, operations: list[dict[str, Any]]) -> dict[str, Any]:
    scenario_dir = _fresh(base / "scenario3_crash_recovery")
    rate = 10
    concurrency = 2
    with LabSession.open(scenario_dir) as session:
        session.write_operations(operations[0:8], records_per_segment=4)

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "replication_lab.crash_worker",
            "--root",
            str(scenario_dir),
            "--rate",
            str(rate),
            "--concurrency",
            str(concurrency),
            "--crash-batch",
            "1",
        ],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        check=False,
    )
    _assert(process.returncode == 99, f"forced termination return code {process.returncode}")

    with LabSession.open(scenario_dir) as session:
        recovery = session.recover()
        _assert(recovery["unknown_batch_count"] == 1, "one unknown batch")
        _assert(recovery["recovered_last_commit_seq"] == 2, "recovered last seq")
        _assert(recovery["rebuilt_sample_count"] == 2, "rebuilt confirmed samples")
        _assert(recovery["claim_exit_report"] is False, "no fabricated exit report")
        resume = session.replay(
            replay_rate=rate, concurrency=concurrency, reset=False
        )
        report = session.observation_report()
        _assert(resume["sample_count"] == 8, "resumed sample count")
        _assert(report["unknown_delay_count"] == 0, "resumed unknown delays")
        snapshot = session.save_snapshot()
        verification = session.verify_snapshot(snapshot["snapshot_id"])
        _assert(verification["ok"], "crash-recovery snapshot verification")

        # Alter the on-disk view, prove verification reports E_REPLAY, then restore
        # the snapshot as a whole replacement.
        session.write_operations(
            [
                {
                    "commit_seq": 9,
                    "txn_id": 1009,
                    "commit_offset_ms": 700,
                    "txn_size_bytes": 4096,
                }
            ],
            records_per_segment=1,
        )
        dirty_verification = session.verify_snapshot(snapshot["snapshot_id"])
        _assert(not dirty_verification["ok"], "dirty view must be irreproducible")
        _assert(dirty_verification["business_code"] == "E_REPLAY", "dirty view E_REPLAY")
        restore = session.restore_snapshot(snapshot["snapshot_id"])
        restored_verification = session.verify_snapshot(snapshot["snapshot_id"])
        _assert(restore["segments_replaced"], "whole-view segment replacement")
        _assert(restored_verification["ok"], "restored snapshot reproducibility")

        bad_parameter_verification = session.verify_snapshot(
            snapshot["snapshot_id"], replay_rate=rate + 1
        )
        _assert(
            bad_parameter_verification["business_code"] == "E_REPLAY",
            "parameter mismatch E_REPLAY",
        )
        scenario = _scenario_summary(
            "强制终止后恢复并重放校验",
            report,
            forced_exit_code=process.returncode,
            recovery=recovery,
            resumed_sample_count=resume["sample_count"],
            snapshot_id=snapshot["snapshot_id"],
            verification_ok=verification["ok"],
            dirty_view_business_code=dirty_verification["business_code"],
            dirty_view_mismatches=dirty_verification["mismatch_items"],
            restore_report=restore,
            restored_verification_ok=restored_verification["ok"],
            parameter_mismatch_business_code=bad_parameter_verification["business_code"],
            parameter_mismatch_items=bad_parameter_verification["mismatch_items"],
            verification_report=restored_verification,
        )
    _write_scenario_artifacts(scenario_dir, scenario)
    return scenario


def _render_demo_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 本地主从复制延迟观测演示报告",
        "",
        f"总体结果: {'通过' if report['ok'] else '失败'}",
        f"输出目录: `{report['output_dir']}`",
        "",
    ]
    for scenario in report["scenarios"]:
        lines.extend(
            [
                f"## {scenario['scenario']}",
                "",
                f"- 版本集合: `{scenario['format_version_set']}`",
                f"- 样本计数: total={scenario['sample_count']} known={scenario['known_delay_count']} unknown={scenario['unknown_delay_count']}",
                f"- 估算状态/置信: `{scenario['estimate_status']}` / `{scenario['confidence']}`",
                f"- 均值定点: `{scenario['mean_fixed']}` (Q={scenario['mean_fixed_scale']})",
                f"- P95 / 最大延迟: `{scenario['p95_ms']} ms` / `{scenario['max_delay_ms']} ms`",
                f"- 结论: {scenario['conclusion']}",
            ]
        )
        if scenario["scenario"] == "格式升级后重新观测":
            lines.append(
                f"- 升级前版本: `{scenario['legacy_format_version_set']}`，升级后: `{scenario['upgraded_format_version_set']}`"
            )
            lines.append(f"- 只读备份数: {len(scenario['backup_files'])}")
        if scenario["scenario"] == "强制终止后恢复并重放校验":
            recovery = scenario["recovery"]
            lines.append(
                f"- 强制退出码/恢复序号/未知批次/重建样本: `{scenario['forced_exit_code']}` / "
                f"`{recovery['recovered_last_commit_seq']}` / `{recovery['unknown_batch_count']}` / "
                f"`{recovery['rebuilt_sample_count']}`"
            )
            lines.append(
                f"- 篡改视图业务码: `{scenario['dirty_view_business_code']}`；参数不一致业务码: `{scenario['parameter_mismatch_business_code']}`"
            )
            lines.append(
                f"- 整视图恢复: `{scenario['restore_report']['replace_mode']}`，恢复后校验: `{scenario['restored_verification_ok']}`"
            )
        lines.append("")
    lines.append("每个场景的完整观测报告位于对应目录的 `scenario.json` 与 `observation.md`。")
    return "\n".join(lines) + "\n"


def run_demo(output: str | Path | None = None) -> dict[str, Any]:
    output_dir = Path(output) if output is not None else PROJECT_ROOT / "artifacts" / "demo"
    output_dir = output_dir.resolve()
    _fresh(output_dir)
    operations = load_operations_file(SAMPLE_OPERATIONS)
    scenarios = [
        _scenario_cold_start(output_dir, operations),
        _scenario_upgrade(output_dir, operations),
        _scenario_crash(output_dir, operations),
    ]
    report = {
        "ok": all(scenario["ok"] for scenario in scenarios),
        "business_code": None,
        "output_dir": str(output_dir),
        "scenario_order": [scenario["scenario"] for scenario in scenarios],
        "scenarios": scenarios,
        "acceptance": {
            "three_version_interop": scenarios[1]["legacy_format_version_set"] == [1, 2, 3]
            and scenarios[1]["upgraded_format_version_set"] == [3],
            "fixed_point_estimation": scenarios[0]["estimate_status"] == "COMPLETE",
            "crash_recovery": scenarios[2]["recovery"]["unknown_batch_count"] == 1
            and scenarios[2]["resumed_sample_count"] == 8,
            "snapshot_whole_replacement": scenarios[2]["restore_report"]["segments_replaced"],
            "replay_business_codes": scenarios[2]["dirty_view_business_code"]
            == "E_REPLAY"
            and scenarios[2]["parameter_mismatch_business_code"] == "E_REPLAY",
        },
    }
    report["rendered_report"] = _render_demo_markdown(report)
    atomic_write_bytes(
        output_dir / "demo_report.json",
        stable_json(report).encode("utf-8") + b"\n",
    )
    atomic_write_bytes(
        output_dir / "demo_report.md",
        report["rendered_report"].encode("utf-8"),
    )
    return report


def main() -> int:
    result = run_demo()
    print(result["rendered_report"], end="")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
