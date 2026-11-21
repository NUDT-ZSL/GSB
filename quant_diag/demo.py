from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import EngineConfig, Injection
from .engine import DiagnosticEngine
from .errors import BusinessError
from .manifest import ManifestView, load_manifest, write_manifest
from .sample_data import write_default_samples
from .selfcheck import run_consistency_checks
from .snapshots import SnapshotStore
from .util import atomic_write_json, read_json


def _fresh(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def ensure_local_samples(workspace: Path) -> tuple[Path, Path]:
    manifest_path = workspace / "model_manifest.jsonl"
    calibration_path = workspace / "calibration_samples.jsonl"
    if not manifest_path.exists() or not calibration_path.exists():
        write_default_samples(workspace)
    return manifest_path, calibration_path


def _run(
    manifest_path: Path,
    calibration_path: Path,
    runtime_dir: Path,
    config: EngineConfig | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    engine = DiagnosticEngine(
        manifest_path=manifest_path,
        calibration_path=calibration_path,
        runtime_dir=runtime_dir,
        config=config,
    )
    report = engine.run()
    return report, engine.latest_snapshot()


def _capture_error(
    manifest_path: Path,
    calibration_path: Path,
    runtime_dir: Path,
    config: EngineConfig,
) -> dict[str, Any]:
    try:
        _run(manifest_path, calibration_path, runtime_dir, config)
    except BusinessError as exc:
        return {"raised": True, "error": exc.as_dict()}
    return {"raised": False}


def _rollback_guard(runtime_dir: Path) -> dict[str, Any]:
    store = SnapshotStore(runtime_dir)
    snapshot = store.load_latest()
    if snapshot is None:
        return {"raised": False, "reason": "no snapshot"}
    try:
        store.commit(
            stage=snapshot["stage"],
            model_tag=snapshot["model_tag"],
            manifest_digest=snapshot["manifest_digest"],
            config_digest=snapshot["config_digest"],
            layer_order=snapshot["layer_order"],
            completed_layers=snapshot["completed_layers"],
            stages=snapshot["stages"],
            dependency=snapshot["dependency"],
            result_digest=snapshot["result_digest"],
            degradations=snapshot.get("degradations", []),
            injections=snapshot.get("injections", []),
            version_hint=int(snapshot["version"]) - 1,
            parent_checksum=snapshot.get("content_checksum"),
        )
    except BusinessError as exc:
        return {"raised": True, "error": exc.as_dict()}
    return {"raised": False}


def _corrupt_snapshot_probe(baseline_runtime: Path, corrupt_runtime: Path) -> dict[str, Any]:
    _fresh(corrupt_runtime)
    shutil.copytree(baseline_runtime, corrupt_runtime, dirs_exist_ok=True)
    current = read_json(corrupt_runtime / "current.json")
    snapshot_path = (
        corrupt_runtime / "snapshots" / f"v{current['version']:012d}.json"
    )
    payload = read_json(snapshot_path)
    payload["result_digest"] = "tampered-without-checksum-update"
    snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    try:
        SnapshotStore(corrupt_runtime).load_latest()
    except BusinessError as exc:
        return {"raised": True, "error": exc.as_dict()}
    return {"raised": False}


def _kill_worker(
    manifest_path: Path,
    calibration_path: Path,
    runtime_dir: Path,
    stage: str,
    layer_index: int,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "quant_diag",
        "_kill-worker",
        "--manifest",
        str(manifest_path),
        "--calibration",
        str(calibration_path),
        "--runtime-dir",
        str(runtime_dir),
        "--kill-stage",
        stage,
        "--kill-layer",
        str(layer_index),
    ]
    return subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def run_demo(
    workspace: Path,
    *,
    runtime_root: Path | None = None,
    output_path: Path | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    workspace = Path(workspace)
    manifest_path, calibration_path = ensure_local_samples(workspace)
    runtime_root = Path(runtime_root or workspace / "runtime" / "demo")
    output_path = Path(output_path or workspace / "artifacts" / "verification_report.json")
    _fresh(runtime_root)
    full_runtime = runtime_root / "full"
    repeat_runtime = runtime_root / "repeat"
    recovery_runtime = runtime_root / "recovery"
    update_runtime = runtime_root / "update"
    corrupt_runtime = runtime_root / "corrupt"
    injected_root = runtime_root / "injected"

    def note(step: str) -> None:
        if progress is not None:
            progress(step)

    note("完整量化推理基线")
    full_report, full_snapshot = _run(
        manifest_path, calibration_path, full_runtime
    )
    self_check = run_consistency_checks(full_snapshot, EngineConfig())
    note("确定性重复运行")
    repeat_engine = DiagnosticEngine(
        manifest_path=manifest_path,
        calibration_path=calibration_path,
        runtime_dir=repeat_runtime,
    )
    repeat_engine.run()
    repeat_snapshot = repeat_engine.latest_snapshot()
    unchanged_report = repeat_engine.run()
    deterministic_equal = (
        full_snapshot["result_digest"] == repeat_snapshot["result_digest"]
    )
    note("强制终止与恢复")
    killed = _kill_worker(
        manifest_path,
        calibration_path,
        recovery_runtime,
        "layer_quantization",
        1,
    )
    recovery_report, recovery_snapshot = _run(
        manifest_path, calibration_path, recovery_runtime
    )
    recovery_equal = (
        full_snapshot["result_digest"] == recovery_snapshot["result_digest"]
    )
    note("权重修订失效与后缀重算")
    update_base_report, update_base_snapshot = _run(
        manifest_path, calibration_path, update_runtime
    )
    manifest_view = load_manifest(manifest_path)
    changed_layer = manifest_view.layers[1]
    updated_layers = list(manifest_view.layers)
    updated_layers[1] = replace(
        changed_layer, weight_revision=changed_layer.weight_revision + 1
    )
    updated_view = ManifestView.from_layers(manifest_view.model_tag, updated_layers)
    updated_manifest_path = runtime_root / "model_manifest_v2.jsonl"
    write_manifest(updated_manifest_path, updated_view)
    update_report, update_snapshot = _run(
        updated_manifest_path, calibration_path, update_runtime
    )
    update_quant = update_snapshot["stages"]["layer_quantization"]
    update_inference = update_snapshot["stages"]["layer_inference"]
    prefix_reused = (
        update_base_snapshot["stages"]["layer_quantization"]["layers"]["layer_0"][
            "checksum"
        ]
        == update_quant["layers"]["layer_0"]["checksum"]
        and update_base_snapshot["stages"]["layer_inference"]["layers"]["layer_0"][
            "checksum"
        ]
        == update_inference["layers"]["layer_0"]["checksum"]
    )
    note("异常注入")
    injection_specs = [
        ("weight_truncate", "layer_1"),
        ("unknown_dtype", "layer_1"),
        ("calibration_count_mismatch", "layer_1"),
        ("inference_input_dim", "layer_0"),
    ]
    injection_results: list[dict[str, Any]] = []
    for kind, layer_id in injection_specs:
        result = _capture_error(
            manifest_path,
            calibration_path,
            injected_root / f"{kind}_{layer_id}",
            EngineConfig(injections=(Injection(kind, layer_id),)),
        )
        result["injection"] = {"kind": kind, "layer_id": layer_id}
        injection_results.append(result)
    tolerance_result = _capture_error(
        manifest_path,
        calibration_path,
        injected_root / "tolerance_zero",
        EngineConfig(tolerance_abs=0.0, tolerance_rel=0.0),
    )
    tolerance_result["injection"] = {"kind": "tolerance_zero", "layer_id": None}
    injection_results.append(tolerance_result)
    note("快照损坏与回退保护")
    corruption = _corrupt_snapshot_probe(full_runtime, corrupt_runtime)
    rollback = _rollback_guard(full_runtime)

    recovery_ok = (
        killed.returncode == 137
        and recovery_equal
        and recovery_report["recovery"]["discarded_uncommitted"] is not None
        and recovery_report["recovery"]["resume_stage"] == "layer_quantization"
        and recovery_report["recovery"]["resume_layer_index"] == 0
    )
    update_ok = (
        update_quant["affected_layers"] == ["layer_1", "layer_2", "layer_3"]
        and update_quant["reused_layers"] == ["layer_0"]
        and update_quant["rebuilt_layers"] == ["layer_1", "layer_2", "layer_3"]
        and update_inference["reused_layers"] == ["layer_0"]
        and prefix_reused
        and update_report["result_digest"] != update_base_report["result_digest"]
    )
    injection_ok = all(item.get("raised", False) for item in injection_results)
    report = {
        "schema": "quant-diag-verification-v1",
        "sample": {
            "manifest": str(manifest_path),
            "calibration": str(calibration_path),
            "manifest_digest": load_manifest(manifest_path).digest,
            "layer_count": len(load_manifest(manifest_path).layers),
            "hardware": "local CPU",
            "third_party_dependency": "numpy",
        },
        "full_run": {
            "version": full_snapshot["version"],
            "result_digest": full_snapshot["result_digest"],
            "final_prediction_digest": full_snapshot["stages"]["layer_inference"][
                "final_prediction_digest"
            ],
            "prediction": full_snapshot["stages"]["layer_inference"][
                "final_prediction"
            ],
            "dominant_source": full_snapshot["stages"]["layer_inference"][
                "diagnostics"
            ]["overall_dominant_source"],
            "final_serial_error": full_snapshot["stages"]["layer_inference"][
                "diagnostics"
            ]["final_serial_error"],
            "stage_versions": full_report["stage_versions"],
            "self_check": self_check,
        },
        "determinism": {
            "passed": deterministic_equal,
            "first_result_digest": full_snapshot["result_digest"],
            "second_result_digest": repeat_snapshot["result_digest"],
            "unchanged_rerun_actions": unchanged_report["actions"],
        },
        "force_kill_recovery": {
            "passed": recovery_ok,
            "kill_exit_code": killed.returncode,
            "killed_stage": "layer_quantization",
            "killed_after_layer_index": 1,
            "baseline_result_digest": full_snapshot["result_digest"],
            "recovered_result_digest": recovery_snapshot["result_digest"],
            "byte_equivalent_result": recovery_equal,
            "recovery_report": recovery_report["recovery"],
            "resume_actions": recovery_report["actions"],
        },
        "revision_update": {
            "passed": update_ok,
            "changed_layer": changed_layer.layer_id,
            "previous_revision": changed_layer.weight_revision,
            "current_revision": changed_layer.weight_revision + 1,
            "affected_layers": update_quant["affected_layers"],
            "reused_layers": update_quant["reused_layers"],
            "rebuilt_layers": update_quant["rebuilt_layers"],
            "inference_reused_layers": update_inference["reused_layers"],
            "prefix_cache_reused": prefix_reused,
            "baseline_result_digest": update_base_report["result_digest"],
            "updated_result_digest": update_report["result_digest"],
            "stage_versions": update_report["stage_versions"],
        },
        "injections": injection_results,
        "snapshot_corruption": corruption,
        "snapshot_rollback_guard": rollback,
        "checks": {
            "full_run_self_check": self_check["passed"],
            "deterministic": deterministic_equal,
            "recovery_matches_full": recovery_equal,
            "revision_suffix_rebuild": update_ok,
            "injections_raise_business_codes": injection_ok,
            "corruption_guard": corruption.get("raised", False),
            "rollback_guard": rollback.get("raised", False),
        },
    }
    report["passed"] = all(report["checks"].values())
    atomic_write_json(output_path, report)
    note("验证报告已写入")
    return report
