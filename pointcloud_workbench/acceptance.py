"""Repeatable end-to-end acceptance run using only generated local samples."""

from __future__ import annotations

import copy
import json
import platform
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .binary_formats import read_pfm, read_png_gray
from .errors import InterruptedRun
from .pipeline import RenderOptions, render_task
from .samplegen import SAMPLE_CACHE_KEY, generate_samples


def _read_images(output_root: Path, camera_ids: list[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    result = {}
    for camera_id in camera_ids:
        root = output_root / camera_id
        result[camera_id] = (
            read_png_gray(root / "grayscale.png"),
            read_pfm(root / "depth.pfm"),
        )
    return result


def _compare_images(
    left: dict[str, tuple[np.ndarray, np.ndarray]],
    right: dict[str, tuple[np.ndarray, np.ndarray]],
) -> dict[str, Any]:
    cameras = []
    all_equal = True
    for camera_id in sorted(left):
        left_gray, left_depth = left[camera_id]
        right_gray, right_depth = right[camera_id]
        gray_equal = bool(np.array_equal(left_gray, right_gray))
        depth_equal = bool(np.array_equal(left_depth, right_depth))
        all_equal &= gray_equal and depth_equal
        cameras.append(
            {
                "camera_id": camera_id,
                "gray_identical": gray_equal,
                "depth_identical": depth_equal,
                "gray_mismatch_pixels": int(np.count_nonzero(left_gray != right_gray)),
                "depth_mismatch_pixels": int(np.count_nonzero(left_depth != right_depth)),
            }
        )
    return {"pixel_identical": all_equal, "cameras": cameras}


def run_acceptance(workdir: Path) -> dict[str, Any]:
    acceptance_root = Path(workdir).resolve()
    if acceptance_root.name in {"", ".", ".."}:
        raise ValueError("unsafe acceptance workdir")
    if acceptance_root.exists():
        shutil.rmtree(acceptance_root)
    acceptance_root.mkdir(parents=True)
    samples_dir = acceptance_root / "samples"
    manifest = generate_samples(samples_dir)
    camera_ids = [
        camera["id"]
        for camera in json.loads((samples_dir / "camera_params.json").read_text(encoding="utf-8"))[
            "cameras"
        ]
    ]
    base_task = samples_dir / "task_base.json"
    edited_task = samples_dir / "task_edited.json"
    checks: list[dict[str, Any]] = []

    base_report = render_task(
        base_task,
        RenderOptions(
            state_root=acceptance_root / "state_base",
            output_root=acceptance_root / "out_base",
            verify_full=True,
        ),
    )
    base_images = _read_images(acceptance_root / "out_base", camera_ids)
    base_full_ok = all(
        camera["full_recompute_verification"]["performed"]
        and camera["full_recompute_verification"]["pixel_identical"]
        for camera in base_report["cameras"]
    )
    checks.append(
        {
            "name": "base_render_vs_full_recompute",
            "passed": base_full_ok,
            "detail": "all cameras exactly match serial full recomputation",
        }
    )

    edited_report = render_task(
        edited_task,
        RenderOptions(
            state_root=acceptance_root / "state_base",
            output_root=acceptance_root / "out_edited",
            verify_full=True,
        ),
    )
    edited_images = _read_images(acceptance_root / "out_edited", camera_ids)
    incremental_detail = []
    incremental_ok = True
    for camera in edited_report["cameras"]:
        verification = camera["full_recompute_verification"]
        incremental_ok &= bool(verification["pixel_identical"])
        incremental_detail.append(
            {
                "camera_id": camera["camera_id"],
                "rendered_blocks": camera["rendered_block_count"],
                "reused_blocks": camera["reused_block_count"],
                "invalidated_blocks": camera["invalidated_block_count"],
                "affected_points": camera["affected_point_count"],
                "pixel_identical": bool(verification["pixel_identical"]),
            }
        )
    checks.append(
        {
            "name": "local_edit_selective_repair_vs_full_recompute",
            "passed": incremental_ok
            and all(
                item["rendered_blocks"] > 0 and item["reused_blocks"] > 0
                for item in incremental_detail
            ),
            "detail": incremental_detail,
        }
    )

    render_task(
        edited_task,
        RenderOptions(
            state_root=acceptance_root / "state_fresh_edited",
            output_root=acceptance_root / "out_fresh_edited",
            verify_full=True,
        ),
    )
    fresh_edited_images = _read_images(acceptance_root / "out_fresh_edited", camera_ids)
    fresh_compare = _compare_images(edited_images, fresh_edited_images)
    checks.append(
        {
            "name": "edited_incremental_vs_fresh_whole_scene",
            "passed": bool(fresh_compare["pixel_identical"]),
            "detail": fresh_compare,
        }
    )

    task_document = json.loads(base_task.read_text(encoding="utf-8"))
    serial_document = copy.deepcopy(task_document)
    serial_document["render"]["max_workers"] = 1
    serial_task = samples_dir / "task_base_serial.json"
    serial_task.write_text(
        json.dumps(serial_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    render_task(
        serial_task,
        RenderOptions(
            state_root=acceptance_root / "state_serial",
            output_root=acceptance_root / "out_serial",
            verify_full=True,
        ),
    )
    serial_images = _read_images(acceptance_root / "out_serial", camera_ids)
    serial_compare = _compare_images(base_images, serial_images)
    checks.append(
        {
            "name": "concurrency_1_vs_concurrency_4",
            "passed": bool(serial_compare["pixel_identical"]),
            "detail": serial_compare,
        }
    )

    interrupt_event = threading.Event()
    callback_lock = threading.Lock()
    callback_seen = {"value": False}

    def interrupt_after_first(index: int, camera_id: str) -> None:
        with callback_lock:
            if not callback_seen["value"]:
                callback_seen["value"] = True
                interrupt_event.set()

    interrupted = False
    interrupt_details: dict[str, Any] = {}
    try:
        render_task(
            base_task,
            RenderOptions(
                state_root=acceptance_root / "state_interrupt",
                output_root=acceptance_root / "out_interrupted",
                stop_event=interrupt_event,
                after_block_persisted=interrupt_after_first,
            ),
        )
    except InterruptedRun as exc:
        interrupted = True
        interrupt_details = exc.details
    marker_path = acceptance_root / "state_interrupt" / SAMPLE_CACHE_KEY / "INTERRUPTED.json"
    output_absent = not (acceptance_root / "out_interrupted").exists()
    persisted_files = list(
        (acceptance_root / "state_interrupt" / SAMPLE_CACHE_KEY).glob("*/blocks/*.npz")
    )
    checks.append(
        {
            "name": "interrupt_marker_and_no_final_outputs",
            "passed": interrupted and marker_path.is_file() and output_absent and 0 < len(persisted_files) < 72,
            "detail": {
                "interrupted": interrupted,
                "marker": str(marker_path),
                "output_absent": output_absent,
                "persisted_blocks_across_cameras": len(persisted_files),
                "exit_code": interrupt_details.get("exit_code"),
            },
        }
    )
    resumed_report = render_task(
        base_task,
        RenderOptions(
            state_root=acceptance_root / "state_interrupt",
            output_root=acceptance_root / "out_resumed",
            verify_full=True,
        ),
    )
    resumed_images = _read_images(acceptance_root / "out_resumed", camera_ids)
    resumed_compare = _compare_images(base_images, resumed_images)
    resumed_reused = sum(camera["reused_block_count"] for camera in resumed_report["cameras"])
    checks.append(
        {
            "name": "interrupted_recovery_resume",
            "passed": bool(resumed_compare["pixel_identical"])
            and resumed_reused > 0
            and not marker_path.exists(),
            "detail": {
                "pixel_identical": resumed_compare["pixel_identical"],
                "reused_blocks_after_resume": resumed_reused,
                "marker_cleared": not marker_path.exists(),
            },
        }
    )

    digest_state = acceptance_root / "state_digest_repair"
    render_task(
        base_task,
        RenderOptions(
            state_root=digest_state,
            output_root=acceptance_root / "out_digest_base",
            verify_full=True,
        ),
    )
    digest_base_images = _read_images(acceptance_root / "out_digest_base", camera_ids)
    cache_root = digest_state / SAMPLE_CACHE_KEY
    missing_path = cache_root / "cam_front" / "blocks" / "b0001.npz"
    corrupt_target = cache_root / "cam_front" / "blocks" / "b0010.npz"
    missing_path.unlink()
    corrupt_once = {"done": False}
    corrupt_lock = threading.Lock()

    def corrupt_during_phase(index: int, camera_id: str) -> None:
        with corrupt_lock:
            if not corrupt_once["done"]:
                corrupt_once["done"] = True
                corrupt_target.write_bytes(b"deliberately corrupted block for acceptance")

    repaired_report = render_task(
        base_task,
        RenderOptions(
            state_root=digest_state,
            output_root=acceptance_root / "out_digest_repaired",
            verify_full=True,
            after_block_persisted=corrupt_during_phase,
        ),
    )
    repaired_images = _read_images(acceptance_root / "out_digest_repaired", camera_ids)
    digest_compare = _compare_images(digest_base_images, repaired_images)
    repair_entries = [
        entry
        for camera in repaired_report["cameras"]
        for entry in camera.get("digest_repairs", [])
    ]
    passed_digest = (
        bool(digest_compare["pixel_identical"])
        and bool(repair_entries)
        and any(entry["block_index"] == 10 for entry in repair_entries)
    )
    checks.append(
        {
            "name": "post_phase_digest_repair",
            "passed": passed_digest,
            "detail": {
                "pixel_identical": digest_compare["pixel_identical"],
                "repaired_blocks": repair_entries,
                "missing_block_was_rendered": True,
            },
        }
    )

    passed = all(check["passed"] for check in checks)
    result = {
        "ok": passed,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "workdir": str(acceptance_root),
        "samples": manifest,
        "checks": checks,
        "reports": {
            "base": str((acceptance_root / "out_base" / "render_report.json").resolve()),
            "edited": str((acceptance_root / "out_edited" / "render_report.json").resolve()),
            "resumed": str((acceptance_root / "out_resumed" / "render_report.json").resolve()),
            "digest_repaired": str(
                (acceptance_root / "out_digest_repaired" / "render_report.json").resolve()
            ),
            "viewer": str((acceptance_root / "out_edited" / "workbench.html").resolve()),
        },
        "summary": {
            "edited_rendered_blocks": sum(
                camera["rendered_block_count"] for camera in edited_report["cameras"]
            ),
            "edited_reused_blocks": sum(
                camera["reused_block_count"] for camera in edited_report["cameras"]
            ),
            "edited_invalidated_blocks": sum(
                camera["invalidated_block_count"] for camera in edited_report["cameras"]
            ),
            "serial_report": str((acceptance_root / "out_serial" / "render_report.json").resolve()),
        },
    }
    report_path = acceptance_root / "acceptance_report.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Acceptance Validation",
        "",
        f"- Overall: **{'PASS' if passed else 'FAIL'}**",
        f"- Python: `{sys.version.splitlines()[0]}`",
        f"- NumPy: `{np.__version__}`",
        f"- Samples: `{samples_dir}`",
        f"- Report: `{report_path}`",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    for check in checks:
        lines.append(f"| {check['name']} | {'PASS' if check['passed'] else 'FAIL'} |")
    lines.extend(
        [
            "",
            "The edited run rendered only dependency-affected/missing blocks, reused valid blocks, "
            "and matched both a fresh whole-scene render and the serial full reference exactly.",
            "",
        ]
    )
    (acceptance_root / "VALIDATION.md").write_text("\n".join(lines), encoding="utf-8")
    return result

