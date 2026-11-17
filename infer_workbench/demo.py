"""Deterministic end-to-end demo covering precision and recovery scenarios."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .assets import load_assets
from .canonical import atomic_write_json
from .config import BatchConfig
from .session import InferenceSession, human_summary


def _make_writable_and_retry(function, path, _excinfo):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    function(path)


def _scenario_line(name: str, report: dict[str, Any], report_path: Path, out_dir: Path) -> str:
    state = report["state"]
    codes = ",".join(report.get("business_codes", [])) or "OK"
    return (
        "scenario={name} config_version={version} precision={precision} "
        "batch_size={batch_size} quant_scale={quant_scale} completed={completed}/{total} "
        "business_codes={codes} report={report}"
    ).format(
        name=name,
        version=report["config_version"],
        precision=report["configuration"]["precision_mode"],
        batch_size=report["configuration"]["batch_size"],
        quant_scale=report["configuration"]["quant_scale"],
        completed=state["completed_batches"],
        total=state["total_batches"],
        codes=codes,
        report=report_path.relative_to(out_dir).as_posix(),
    )


def run_demo(*, project_root: Path, out_dir: Path, clean: bool = True) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    out_dir = Path(out_dir).resolve()
    if clean and out_dir.exists():
        shutil.rmtree(out_dir, onerror=_make_writable_and_retry)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = out_dir / "reports"
    snapshots_dir = out_dir / "snapshots"
    reports_dir.mkdir(parents=True, exist_ok=True)
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    assets = load_assets(project_root / "data")
    float_config = BatchConfig.load(project_root / "configs" / "float32_b4.json")
    fixed_q3_config = BatchConfig.load(project_root / "configs" / "fixed_q3_b3.json")
    fixed_q2_config = BatchConfig.load(project_root / "configs" / "fixed_q2_b3.json")

    session = InferenceSession(assets, snapshot_dir=snapshots_dir)
    summary_lines = [
        f"data_digest={assets.data_digest}",
        f"parameter_digest={assets.parameter_digest}",
    ]
    scenario_reports: dict[str, dict[str, Any]] = {}

    report_float = session.run(float_config)
    path_float = reports_dir / "01_float32.json"
    atomic_write_json(path_float, report_float)
    scenario_reports["float32"] = report_float
    summary_lines.append(_scenario_line("01_float32", report_float, path_float, out_dir))

    partial_snapshot = snapshots_dir / "fixed_q3_partial.json"
    report_q3 = session.run(
        fixed_q3_config,
        stop_after=2,
        snapshot_path=partial_snapshot,
    )
    path_q3 = reports_dir / "02_fixed_q3_interrupted.json"
    atomic_write_json(path_q3, report_q3)
    scenario_reports["fixed_q3_partial"] = report_q3
    summary_lines.append(_scenario_line("02_fixed_q3_interrupted", report_q3, path_q3, out_dir))
    summary_lines.append(
        "  snapshot={} next_batch_index={}".format(
            partial_snapshot.relative_to(out_dir).as_posix(),
            report_q3["state"]["next_batch_index"],
        )
    )

    report_q2 = session.run(fixed_q2_config)
    path_q2 = reports_dir / "03_fixed_q2_replaced.json"
    atomic_write_json(path_q2, report_q2)
    scenario_reports["fixed_q2"] = report_q2
    summary_lines.append(_scenario_line("03_fixed_q2_replaced", report_q2, path_q2, out_dir))
    transition = report_q2.get("version_transition") or {}
    summary_lines.append(
        "  version_diff={}->{} changed_batches={} changed_layers={} drift_direction={}".format(
            transition.get("old_config_version"),
            transition.get("new_config_version"),
            ",".join(str(value) for value in transition.get("changed_batch_indices", [])) or "none",
            ",".join(transition.get("changed_layers", [])) or "none",
            transition.get("drift_direction"),
        )
    )

    restore = session.restore_snapshot(partial_snapshot)
    if restore.get("code"):
        raise RuntimeError(f"demo restore failed: {restore}")
    replay = session.replay_snapshot(partial_snapshot, expected_config=fixed_q3_config)
    if replay.get("code"):
        raise RuntimeError(f"demo replay failed: {replay}")
    if not session.complete:
        session.continue_remaining(snapshot_path=partial_snapshot)
    report_restore = session.build_report()
    path_restore = reports_dir / "04_restore_replay_continue.json"
    atomic_write_json(path_restore, report_restore)
    scenario_reports["restore_replay"] = report_restore
    summary_lines.append(
        _scenario_line("04_restore_replay_continue", report_restore, path_restore, out_dir)
    )
    summary_lines.append(
        "  restore_status={} readonly_backup={}".format(
            restore.get("status"), restore.get("readonly_backup")
        )
    )
    summary_lines.append(
        "  replay_status={} checked_batches={} checked_samples={} first_mismatch={}".format(
            replay.get("status"),
            replay.get("checked_batches"),
            replay.get("checked_samples"),
            replay.get("first_mismatch") or "none",
        )
    )
    final_snapshot = session.save_snapshot(partial_snapshot)
    summary_lines.append(
        "  resumed_snapshot={} completed_batches={}".format(
            final_snapshot.relative_to(out_dir).as_posix(), len(session.batches)
        )
    )

    manifest = {
        "schema": "infer-workbench.demo.v1",
        "data_digest": assets.data_digest,
        "parameter_digest": assets.parameter_digest,
        "versions": {
            "float32": float_config.config_version,
            "fixed_q3": fixed_q3_config.config_version,
            "fixed_q2": fixed_q2_config.config_version,
        },
        "scenarios": [
            "01_float32",
            "02_fixed_q3_interrupted",
            "03_fixed_q2_replaced",
            "04_restore_replay_continue",
        ],
        "restore": restore,
        "replay": replay,
        "snapshots": {
            "partial_then_resumed": partial_snapshot.relative_to(out_dir).as_posix(),
        },
    }
    manifest_path = out_dir / "demo_manifest.json"
    atomic_write_json(manifest_path, manifest)
    summary = "\n".join(summary_lines) + "\n"
    (out_dir / "demo_summary.txt").write_text(summary, encoding="utf-8", newline="\n")
    return {
        "status": "ok",
        "summary": summary,
        "manifest_path": manifest_path,
        "reports": scenario_reports,
    }
