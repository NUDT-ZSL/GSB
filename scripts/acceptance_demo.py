#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latentflow.errors import BusinessError, ErrorCode  # noqa: E402
from latentflow.service import PreviewService  # noqa: E402
from latentflow.snapshot import SnapshotStore  # noqa: E402


def _square(value: int) -> int:
    return value * value


def business_error(exc: BusinessError) -> dict[str, Any]:
    return {"code": exc.code.value, "message": exc.message, "details": exc.details}


def run_job(
    service: PreviewService,
    config: dict[str, Any],
    compare_block_size: int,
) -> dict[str, Any]:
    start = service.create_job(
        {"config": config, "compare_block_size": compare_block_size}
    )
    job_id = start["job"]["job_id"]
    chunks = []
    for index in range(start["job"]["plan"]["num_blocks"]):
        chunks.append(service.decode_chunk(job_id, index))
    completion = service.complete_job(job_id, compare_block_size)
    return {"start": start, "chunks": chunks, "completion": completion}


def main() -> int:
    report: dict[str, Any] = {
        "application": "LatentFlow Studio",
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
            "execution": "local process, no container, no remote data",
            "cpu_count": os.cpu_count(),
            "disk_free_bytes": shutil.disk_usage(ROOT).free,
        },
        "cases": {},
    }

    try:
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=2, mp_context=context
        ) as executor:
            process_results = sorted(executor.map(_square, [7, 9]))
        report["environment"]["multiprocess_probe"] = {
            "passed": process_results == [49, 81],
            "results": process_results,
        }
    except Exception as exc:  # pragma: no cover - host capability probe
        report["environment"]["multiprocess_probe"] = {
            "passed": False,
            "error": str(exc),
        }
    report["all_passed"] = report["environment"]["multiprocess_probe"]["passed"]

    with tempfile.TemporaryDirectory() as directory:
        temp_root = Path(directory)
        (temp_root / "samples").mkdir(parents=True)
        shutil.copyfile(
            ROOT / "samples" / "reference.ppm",
            temp_root / "samples" / "reference.ppm",
        )
        service = PreviewService(temp_root)
        base = {
            "prompt": "赤色山脊上的缓慢镜头，薄雾与晨光",
            "seed": 20261120,
            "width": 192,
            "height": 108,
            "frames": 12,
            "steps": 4,
            "latency_budget_ms": 500.0,
            "anchor_path": "samples/reference.ppm",
            "anchor_strength": 0.32,
            "max_concurrent_decode_tasks": 4,
        }

        first_config = {**base, "block_size": 4}
        first = run_job(service, first_config, 3)
        completion = first["completion"]
        first_block = first["chunks"][0]
        report["cases"]["streaming_preview"] = {
            "passed": bool(
                completion["consistency"]["equal"]
                and completion["alternative"]["consistency"]["equal"]
                and completion["anchor_validation"]["equal"]
            ),
            "job_id": completion["job"]["job_id"],
            "selected_block_size": completion["job"]["plan"]["selected_block_size"],
            "num_blocks": completion["job"]["plan"]["num_blocks"],
            "latency_budget_ms": completion["job"]["plan"]["latency_budget_ms"],
            "estimated_first_block_ms": completion["job"]["plan"]["estimated_first_block_ms"],
            "first_block_actual_ms": first_block["block"]["timing"]["total_ms"],
            "first_block_stage_ms": first_block["block"]["timing"]["stage_ms"],
            "ready_chunks": first_block["job"]["ready"]["chunk_count"],
            "ready_frames": first_block["job"]["ready"]["frames"],
            "preview_vs_one_shot": completion["consistency"],
            "alternate_block_size": completion["alternative"]["block_size"],
            "alternate_vs_one_shot": completion["alternative"]["consistency"],
            "anchor_validation": completion["anchor_validation"],
            "video_sha256": completion["whole"]["sha256"],
            "snapshot_versions": completion["snapshot_versions"],
        }

        second_config = {**base, "block_size": 6}
        second = run_job(service, second_config, 2)
        second_completion = second["completion"]
        same_hash = (
            second_completion["whole"]["sha256"]
            == completion["whole"]["sha256"]
        )
        report["cases"]["cross_block_reproducibility"] = {
            "passed": bool(
                same_hash
                and second_completion["consistency"]["equal"]
                and second_completion["alternative"]["consistency"]["equal"]
            ),
            "first_run_block_size": 4,
            "second_run_block_size": 6,
            "first_video_sha256": completion["whole"]["sha256"],
            "second_video_sha256": second_completion["whole"]["sha256"],
            "byte_identical": same_hash,
            "second_first_block_actual_ms": second["chunks"][0]["block"]["timing"]["total_ms"],
        }

        over_budget_config = {
            **base,
            "block_size": 8,
            "latency_budget_ms": 0.0001,
        }
        try:
            service.create_job({"config": over_budget_config})
        except BusinessError as exc:
            report["cases"]["over_budget"] = {
                "passed": exc.code == ErrorCode.E_LATENCY_BUDGET,
                "error": business_error(exc),
            }
        else:
            report["cases"]["over_budget"] = {
                "passed": False,
                "error": "expected E_LATENCY_BUDGET",
            }

        fault_config = {**base, "block_size": 4}
        start = service.create_job(
            {"config": fault_config, "fault_anchor": True}
        )
        try:
            service.decode_chunk(start["job"]["job_id"], 0)
        except BusinessError as exc:
            report["cases"]["anchor_fault"] = {
                "passed": exc.code == ErrorCode.E_ANCHOR_MISMATCH,
                "error": business_error(exc),
            }
        else:
            report["cases"]["anchor_fault"] = {
                "passed": False,
                "error": "expected E_ANCHOR_MISMATCH",
            }

        invalid_config = {**base, "anchor_strength": 1.5}
        try:
            service.create_job({"config": invalid_config})
        except BusinessError as exc:
            report["cases"]["invalid_anchor_strength"] = {
                "passed": exc.code == ErrorCode.E_CONFIG_INVALID,
                "error": business_error(exc),
            }
        else:
            report["cases"]["invalid_anchor_strength"] = {
                "passed": False,
                "error": "expected E_CONFIG_INVALID",
            }

        resource_config = {
            **base,
            "frames": 13,
            "max_frames": 12,
        }
        try:
            service.create_job({"config": resource_config})
        except BusinessError as exc:
            report["cases"]["resource_limit"] = {
                "passed": exc.code == ErrorCode.E_RESOURCE_LIMIT,
                "error": business_error(exc),
            }
        else:
            report["cases"]["resource_limit"] = {
                "passed": False,
                "error": "expected E_RESOURCE_LIMIT",
            }

        lease_config = {**base, "frames": 4, "block_size": 2}
        lease_start = service.create_job({"config": lease_config})
        try:
            service.create_job({"config": {**lease_config, "seed": 1}})
        except BusinessError as exc:
            busy_error = business_error(exc)
            busy_passed = exc.code == ErrorCode.E_STAGE_BUSY
        else:
            busy_error = "expected E_STAGE_BUSY"
            busy_passed = False
        lease_job = lease_start["job"]["job_id"]
        for index in range(lease_start["job"]["plan"]["num_blocks"]):
            service.decode_chunk(lease_job, index)
        service.complete_job(lease_job, 3)
        report["cases"]["stage_busy"] = {
            "passed": busy_passed,
            "error": busy_error,
        }

        with tempfile.TemporaryDirectory() as snapshot_dir:
            store = SnapshotStore(Path(snapshot_dir))
            snapshot = store.commit("decode_params", {"steps": 4, "seed": 7})
            active = Path(snapshot_dir) / "active-decode_params.json"
            parsed = json.loads(active.read_text("utf-8"))
            parsed["payload"]["steps"] = 5
            active.write_text(json.dumps(parsed), encoding="utf-8")
            try:
                store.load_all()
            except BusinessError as exc:
                report["cases"]["snapshot_corruption"] = {
                    "passed": (
                        exc.code == ErrorCode.E_SNAPSHOT_CORRUPT
                        and exc.details.get("category") == "decode_params"
                        and exc.details.get("version") == snapshot.version
                    ),
                    "error": business_error(exc),
                }
            else:
                report["cases"]["snapshot_corruption"] = {
                    "passed": False,
                    "error": "expected E_SNAPSHOT_CORRUPT",
                }

    report["all_passed"] = report["all_passed"] and all(
        bool(case.get("passed")) for case in report["cases"].values()
    )
    output = ROOT / "artifacts" / "acceptance-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\nreport: {output}")
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
