"""Run the complete local acceptance demo and write validation/summary.json."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import PIL
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recon_workbench.injection import load_injections
from recon_workbench.pipeline import ReconstructionWorkbench
from recon_workbench.samples import generate_sample_dataset, materialize_injections


SAMPLE = ROOT / "sample_data"
CONFIG = ROOT / "configs" / "default.json"
INJECTION = ROOT / "configs" / "injection.json"
OUT = ROOT / "validation" / "acceptance"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run_engine(
    *,
    name: str,
    config: Path,
    input_dir: Path = SAMPLE,
    injection: Path | None = None,
    profile: str | None = None,
    cache: Path | None = None,
    resume: bool = True,
    state: Path | None = None,
):
    root = OUT / name
    state_root = state if state is not None else root / "state"
    workbench = ReconstructionWorkbench(
        input_dir=input_dir,
        config_path=config,
        injection_path=injection,
        injection_profile=profile,
        state_root=state_root,
        cache_root=cache,
        report_path=root / "report.json",
        cloud_out_path=root / "cloud.json",
        depth_out_path=root / "depth_layers.json",
        features_out_path=root / "features.json",
        matches_out_path=root / "matches.json",
        resume=resume,
    )
    return workbench.run(), root


def cache_counts(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {
        kind: {
            "reused": int(stats["reused"]),
            "recomputed": int(stats["recomputed"]),
            "invalid": int(stats["invalid"]),
        }
        for kind, stats in report["run"]["cache"].items()
    }


def prune_acceptance_outputs() -> None:
    """Keep concise evidence, not multi-run caches and copied input images."""
    copied_inputs = {
        "image_changed_input",
        "camera_changed_input",
        "materialized_injected_input",
        "repro_sample",
    }
    baseline_keep = {
        "report.json",
        "cloud.json",
        "features.json",
        "matches.json",
        "depth_layers.json",
    }
    common_keep = {"report.json", "cloud.json", "interrupt_report.json"}
    for directory in sorted(OUT.iterdir()):
        if not directory.is_dir():
            continue
        if directory.name in copied_inputs:
            shutil.rmtree(directory)
            continue
        for state_name in ("state", "cache"):
            candidate = directory / state_name
            if candidate.exists():
                shutil.rmtree(candidate)
        keep = baseline_keep if directory.name == "baseline" else common_keep
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
                continue
            if child.name not in keep:
                child.unlink()


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    if not SAMPLE.exists():
        generate_sample_dataset(
            SAMPLE,
            seed=20261117,
            view_count=8,
            width=640,
            height=480,
            marker_count=72,
        )
    repro_sample = OUT / "repro_sample"
    generate_sample_dataset(
        repro_sample,
        seed=20261117,
        view_count=8,
        width=640,
        height=480,
        marker_count=72,
    )
    original_tree = tree_sha256(SAMPLE)
    reproduced_tree = tree_sha256(repro_sample)
    sample_reproducible = original_tree == reproduced_tree

    default_payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    serial_payload = json.loads(json.dumps(default_payload))
    serial_payload["executor"]["max_workers"] = 1
    serial_config = OUT / "serial_config.json"
    serial_config.write_text(json.dumps(serial_payload, indent=2), encoding="utf-8")

    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, detail: Any = None) -> None:
        checks.append({"name": name, "passed": bool(condition), "detail": detail})

    baseline, baseline_root = run_engine(name="baseline", config=CONFIG)
    baseline_result = baseline.report["result"]
    check(
        "fixed seed regenerates byte-identical sample files",
        sample_reproducible,
        {"files_compared": len(original_tree)},
    )
    check("baseline completed", baseline.status == "completed")
    check("baseline exit code 0", baseline.exit_code == 0)
    check("baseline no business failures", baseline_result["business_codes"] == [])
    check("baseline produces points", baseline_result["reconstructed_point_count"] > 0)
    check("baseline produces voxels", baseline_result["voxel_count"] > 0)
    check("baseline all 8 views valid", all(v["status"] == "ok" for v in baseline_result["views"]))

    resume, resume_root = run_engine(
        name="resume",
        config=CONFIG,
        state=baseline_root / "state",
    )
    resume_cache = cache_counts(resume.report)
    check("same-state resume result identical", resume.report["result"] == baseline_result)
    check(
        "same-state resume recomputes nothing",
        all(stats["recomputed"] == 0 for stats in resume_cache.values()),
        resume_cache,
    )
    check(
        "same-state resume cloud bytes identical",
        (baseline_root / "cloud.json").read_bytes() == (resume_root / "cloud.json").read_bytes(),
    )

    serial, serial_root = run_engine(name="serial", config=serial_config)
    check("serial result identical", serial.report["result"] == baseline_result)
    for filename in ("cloud.json", "depth_layers.json", "features.json", "matches.json"):
        check(
            f"serial {filename} bytes identical",
            (baseline_root / filename).read_bytes() == (serial_root / filename).read_bytes(),
        )

    mismatch, mismatch_root = run_engine(
        name="config_mismatch",
        config=serial_config,
        state=baseline_root / "state",
    )
    check("config mismatch rejected", mismatch.status == "rejected")
    check("config mismatch exit code 3", mismatch.exit_code == 3)
    check(
        "config mismatch business code",
        mismatch.report["result"]["business_codes"] == ["E_CONFIG_MISMATCH"],
    )

    image_input = OUT / "image_changed_input"
    shutil.copytree(SAMPLE, image_input)
    image_path = image_input / "views" / "view_0004.png"
    with Image.open(image_path) as image:
        array = np.asarray(image.convert("RGB")).copy()
    array[0, 0] = np.array([255, 0, 255], dtype=np.uint8)
    Image.fromarray(array, mode="RGB").save(image_path, format="PNG", compress_level=6)
    (image_input / "sample_manifest.json").unlink()
    image_changed, _ = run_engine(
        name="cache_image_changed",
        config=CONFIG,
        input_dir=image_input,
        cache=baseline_root / "state" / "cache",
        resume=False,
    )
    image_cache = cache_counts(image_changed.report)
    check(
        "image change recalculates one feature set",
        image_cache["features"] == {"reused": 7, "recomputed": 1, "invalid": 0},
        image_cache,
    )
    check(
        "image change recalculates only dependent pairs",
        image_cache["pairs"] == {"reused": 6, "recomputed": 3, "invalid": 0},
        image_cache,
    )
    check(
        "image change recalculates only dependent depth layers",
        image_cache["depth"] == {"reused": 4, "recomputed": 4, "invalid": 0},
        image_cache,
    )
    check(
        "image change recalculates global cloud",
        image_cache["cloud"] == {"reused": 0, "recomputed": 1, "invalid": 0},
        image_cache,
    )

    camera_input = OUT / "camera_changed_input"
    shutil.copytree(SAMPLE, camera_input)
    camera_path = camera_input / "cameras" / "view_0004.json"
    camera = json.loads(camera_path.read_text(encoding="utf-8"))
    camera["intrinsics"]["cx"] += 2.0
    camera_path.write_text(
        json.dumps(camera, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (camera_input / "sample_manifest.json").unlink()
    camera_changed, _ = run_engine(
        name="cache_camera_changed",
        config=CONFIG,
        input_dir=camera_input,
        cache=baseline_root / "state" / "cache",
        resume=False,
    )
    camera_cache = cache_counts(camera_changed.report)
    check(
        "camera change has expected dependency scope",
        camera_cache["features"]
        == {"reused": 7, "recomputed": 1, "invalid": 0}
        and camera_cache["pairs"] == {"reused": 6, "recomputed": 3, "invalid": 0}
        and camera_cache["depth"] == {"reused": 4, "recomputed": 4, "invalid": 0}
        and camera_cache["cloud"] == {"reused": 0, "recomputed": 1, "invalid": 0},
        camera_cache,
    )

    depth_payload = json.loads(json.dumps(default_payload))
    depth_payload["depth_map"]["smoothing_iterations"] += 1
    depth_config = OUT / "depth_param_config.json"
    depth_config.write_text(json.dumps(depth_payload, indent=2), encoding="utf-8")
    depth_changed, _ = run_engine(
        name="cache_depth_param_changed",
        config=depth_config,
        cache=baseline_root / "state" / "cache",
        resume=False,
    )
    depth_cache = cache_counts(depth_changed.report)
    check(
        "depth parameter change reuses features and pairs",
        depth_cache["features"]["recomputed"] == 0
        and depth_cache["pairs"]["recomputed"] == 0,
        depth_cache,
    )
    check(
        "depth parameter change recalculates all depth layers and cloud",
        depth_cache["depth"]["recomputed"] == 8 and depth_cache["cloud"]["recomputed"] == 1,
        depth_cache,
    )

    recoverable, recoverable_root = run_engine(
        name="injection_recoverable",
        config=CONFIG,
        injection=INJECTION,
        profile="recoverable",
    )
    recoverable_result = recoverable.report["result"]
    recoverable_hits = {item["id"] for item in recoverable.report["injection"]["hits"]}
    check("recoverable injection run completes", recoverable.status == "completed")
    check(
        "recoverable business codes",
        set(recoverable_result["business_codes"])
        == {"E_VIEW_INVALID", "E_MATCH_INSUFFICIENT", "E_NONFINITE_FEATURE"},
        recoverable_result["business_codes"],
    )
    check(
        "recoverable injection ids hit",
        recoverable_hits == {"INJ-PNG-001", "INJ-CAM-002", "INJ-PAR-003", "INJ-NAN-004"},
        sorted(recoverable_hits),
    )

    stage_exceptions, _ = run_engine(
        name="injection_stage_exceptions",
        config=CONFIG,
        injection=INJECTION,
        profile="stage_exceptions",
    )
    check(
        "stage exceptions recorded and siblings continue",
        stage_exceptions.status == "completed"
        and "E_STAGE_EXCEPTION" in stage_exceptions.report["result"]["business_codes"]
        and {item["id"] for item in stage_exceptions.report["injection"]["hits"]}
        == {"INJ-EXC-F-005", "INJ-EXC-M-006", "INJ-EXC-D-007"},
    )

    config_invalid, _ = run_engine(
        name="injection_config_invalid",
        config=CONFIG,
        injection=INJECTION,
        profile="config_invalid",
    )
    check(
        "config invalid rejected with exit 2",
        config_invalid.status == "config_invalid" and config_invalid.exit_code == 2,
    )
    check(
        "config invalid injection ids hit",
        {item["id"] for item in config_invalid.report["injection"]["hits"]}
        == {"INJ-VOX-008", "INJ-WRK-009"},
    )

    injected_input = OUT / "materialized_injected_input"
    materialize_injections(
        SAMPLE,
        injected_input,
        load_injections(INJECTION, "recoverable"),
    )
    materialized, _ = run_engine(
        name="injection_materialized",
        config=CONFIG,
        input_dir=injected_input,
        injection=INJECTION,
        profile="recoverable",
    )
    check(
        "materialized corrupt PNG and missing camera are handled",
        materialized.status == "completed"
        and "E_VIEW_INVALID" in materialized.report["result"]["business_codes"]
        and {item["id"] for item in materialized.report["injection"]["hits"]}
        == {"INJ-PNG-001", "INJ-CAM-002", "INJ-PAR-003", "INJ-NAN-004"},
    )

    sigint_root = OUT / "graceful_sigint"
    sigint_state = sigint_root / "state"
    sigint_report = sigint_root / "interrupt_report.json"
    sigint_command = [
        sys.executable,
        "-m",
        "recon_workbench",
        "run",
        "--input",
        str(SAMPLE),
        "--config",
        str(CONFIG),
        "--state",
        str(sigint_state),
        "--report",
        str(sigint_report),
        "--cloud-out",
        str(sigint_root / "cloud_partial.json"),
        "--depth-out",
        str(sigint_root / "depth_partial.json"),
        "--features-out",
        str(sigint_root / "features_partial.json"),
        "--matches-out",
        str(sigint_root / "matches_partial.json"),
        "--test-delay-ms",
        "120",
    ]
    sigint_process = subprocess.Popen(
        sigint_command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        snapshots = (
            sorted((sigint_state / "snapshots").glob("snapshot-*.json"))
            if (sigint_state / "snapshots").exists()
            else []
        )
        if len(snapshots) >= 2:
            break
        if sigint_process.poll() is not None:
            break
        time.sleep(0.01)
    if sigint_process.poll() is None:
        if os.name == "nt":
            sigint_process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            sigint_process.send_signal(signal.SIGINT)
    sigint_stdout, sigint_stderr = sigint_process.communicate(timeout=30)
    interrupted_json = json.loads(sigint_report.read_text(encoding="utf-8"))
    check(
        "graceful signal exits 0 with interrupted report",
        sigint_process.returncode == 0
        and interrupted_json["status"] == "interrupted"
        and interrupted_json["run"]["latest_snapshot_sequence"] > 0,
        {"stdout": sigint_stdout, "stderr": sigint_stderr},
    )
    sigint_resumed, _ = run_engine(
        name="graceful_sigint_resumed",
        config=CONFIG,
        state=sigint_state,
    )
    check(
        "graceful signal resume result identical",
        sigint_resumed.report["result"] == baseline_result,
    )

    kill_root = OUT / "forced_kill"
    kill_state = kill_root / "state"
    kill_report = kill_root / "no_report_expected.json"
    kill_command = [
        sys.executable,
        "-m",
        "recon_workbench",
        "run",
        "--input",
        str(SAMPLE),
        "--config",
        str(CONFIG),
        "--state",
        str(kill_state),
        "--report",
        str(kill_report),
        "--cloud-out",
        str(kill_root / "cloud_partial.json"),
        "--depth-out",
        str(kill_root / "depth_partial.json"),
        "--test-delay-ms",
        "150",
    ]
    kill_process = subprocess.Popen(
        kill_command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    observed_kill: dict[str, Any] | None = None
    deadline = time.time() + 20
    while time.time() < deadline:
        snapshots = (
            sorted((kill_state / "snapshots").glob("snapshot-*.json"))
            if (kill_state / "snapshots").exists()
            else []
        )
        if snapshots:
            observed_kill = json.loads(snapshots[-1].read_text(encoding="utf-8"))
            if observed_kill["stage"] == "matching" and not observed_kill["stage_complete"]:
                break
        if kill_process.poll() is not None:
            break
        time.sleep(0.01)
    if kill_process.poll() is None:
        kill_process.kill()
    kill_process.communicate(timeout=30)
    check(
        "forced kill leaves no final report",
        kill_process.returncode != 0 and not kill_report.exists(),
    )
    check(
        "forced kill stopped at a partial matching snapshot",
        observed_kill is not None
        and observed_kill.get("stage") == "matching"
        and not observed_kill.get("stage_complete", True),
        {
            "sequence": observed_kill.get("sequence") if observed_kill else None,
            "stage": observed_kill.get("stage") if observed_kill else None,
        },
    )
    kill_resumed, _ = run_engine(
        name="forced_kill_resumed",
        config=CONFIG,
        state=kill_state,
    )
    check(
        "forced-kill resume result identical",
        kill_resumed.report["result"] == baseline_result,
    )
    check(
        "forced-kill resume cloud bytes identical",
        (baseline_root / "cloud.json").read_bytes()
        == (OUT / "forced_kill_resumed" / "cloud.json").read_bytes(),
    )
    check(
        "forced-kill resume actually reused pair artifacts",
        kill_resumed.report["run"]["cache"]["pairs"]["reused"] > 0,
        cache_counts(kill_resumed.report),
    )

    summary = {
        "schema_version": 1,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pillow": PIL.__version__,
            "platform": platform.platform(),
        },
        "sample": {
            "path": str(SAMPLE.relative_to(ROOT)),
            "view_count": 8,
            "resolution": [640, 480],
            "seed": 20261117,
            "reproducible": sample_reproducible,
            "reproducibility_files_compared": len(original_tree),
        },
        "baseline": {
            "status": baseline.status,
            "exit_code": baseline.exit_code,
            "result_digest": baseline_result["result_digest"],
            "views": [
                {
                    "view_index": view["view_index"],
                    "feature_count": view["feature_count"],
                    "match_pair_count": view["match_pair_count"],
                    "reconstructed_point_count": view["reconstructed_point_count"],
                    "depth_nonzero_cells": view["depth_nonzero_cells"],
                }
                for view in baseline_result["views"]
            ],
            "match_pair_count": baseline_result["match_pair_count"],
            "reconstructed_point_count": baseline_result["reconstructed_point_count"],
            "voxel_count": baseline_result["voxel_count"],
            "cloud_digest": baseline_result["cloud_digest"],
            "snapshot_version": baseline_result["snapshot_version"],
            "artifacts_sha256": {
                "cloud": file_sha256(baseline_root / "cloud.json"),
                "features": file_sha256(baseline_root / "features.json"),
                "matches": file_sha256(baseline_root / "matches.json"),
                "depth_layers": file_sha256(baseline_root / "depth_layers.json"),
                "report": file_sha256(baseline_root / "report.json"),
            },
        },
        "resume": {
            "result_equal": resume.report["result"] == baseline_result,
            "cache": resume_cache,
            "recovery_point": resume.report["run"]["recovery_point"],
            "latest_snapshot": resume.report["run"]["latest_snapshot_sequence"],
        },
        "serial_equivalence": {
            "result_equal": serial.report["result"] == baseline_result,
            "cloud_bytes_equal": (baseline_root / "cloud.json").read_bytes()
            == (serial_root / "cloud.json").read_bytes(),
            "features_bytes_equal": (baseline_root / "features.json").read_bytes()
            == (serial_root / "features.json").read_bytes(),
            "matches_bytes_equal": (baseline_root / "matches.json").read_bytes()
            == (serial_root / "matches.json").read_bytes(),
            "depth_bytes_equal": (baseline_root / "depth_layers.json").read_bytes()
            == (serial_root / "depth_layers.json").read_bytes(),
        },
        "cache_invalidation": {
            "image_changed": image_cache,
            "camera_changed": camera_cache,
            "depth_parameter_changed": depth_cache,
        },
        "injections": {
            "recoverable": {
                "status": recoverable.status,
                "codes": recoverable_result["business_codes"],
                "hits": sorted(recoverable_hits),
            },
            "stage_exceptions": {
                "status": stage_exceptions.status,
                "codes": stage_exceptions.report["result"]["business_codes"],
                "hits": sorted(
                    item["id"] for item in stage_exceptions.report["injection"]["hits"]
                ),
            },
            "config_invalid": {
                "status": config_invalid.status,
                "exit_code": config_invalid.exit_code,
                "codes": config_invalid.report["result"]["business_codes"],
                "hits": sorted(
                    item["id"] for item in config_invalid.report["injection"]["hits"]
                ),
            },
            "materialized_files": {
                "status": materialized.status,
                "codes": materialized.report["result"]["business_codes"],
                "hits": sorted(
                    item["id"] for item in materialized.report["injection"]["hits"]
                ),
            },
        },
        "snapshot_mismatch": {
            "status": mismatch.status,
            "exit_code": mismatch.exit_code,
            "codes": mismatch.report["result"]["business_codes"],
        },
        "graceful_sigint": {
            "exit_code": sigint_process.returncode,
            "status": interrupted_json["status"],
            "snapshot_sequence": interrupted_json["run"]["latest_snapshot_sequence"],
            "resume_result_equal": sigint_resumed.report["result"] == baseline_result,
        },
        "forced_kill": {
            "returncode": kill_process.returncode,
            "report_exists": kill_report.exists(),
            "snapshot_sequence": observed_kill.get("sequence") if observed_kill else None,
            "snapshot_stage": observed_kill.get("stage") if observed_kill else None,
            "snapshot_stage_complete": (
                observed_kill.get("stage_complete") if observed_kill else None
            ),
            "resume_result_equal": kill_resumed.report["result"] == baseline_result,
            "resume_cache": cache_counts(kill_resumed.report),
        },
        "checks": checks,
        "all_checks_passed": all(item["passed"] for item in checks),
    }
    summary_path = OUT / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    prune_acceptance_outputs()
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if summary["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
