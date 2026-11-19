"""End-to-end crash/restart demonstration using a real child process."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes
from .config import AnalysisConfig
from .engine import AnalysisEngine
from .errors import BusinessError, E_CONFIG_INVALID, E_INCONSISTENT
from .selfcheck import run_selfcheck
from .snapshot import STAGE_ORDER
from .synth import generate_dataset


def run_demo(
    workdir: Path,
    *,
    stop_after: str = "segmentation",
    config: AnalysisConfig | None = None,
    dialog_count: int = 4,
    speaker_count: int = 3,
) -> dict[str, Any]:
    if stop_after not in STAGE_ORDER:
        raise BusinessError(
            E_CONFIG_INVALID,
            "stop_after must name a stage",
            {"field": "stop_after", "actual": stop_after, "allowed": list(STAGE_ORDER)},
        )
    demo_root = workdir / "demo"
    dataset_dir = demo_root / "dataset"
    full_state = demo_root / "full_state"
    resumed_state = demo_root / "resume_state"
    for path in (full_state, resumed_state):
        if path.exists():
            shutil.rmtree(path)
    demo_root.mkdir(parents=True, exist_ok=True)
    generate_dataset(
        dataset_dir,
        dialog_count=dialog_count,
        speaker_count=speaker_count,
        seed=20261120,
        force=True,
    )
    cfg = config or AnalysisConfig()

    full_engine = AnalysisEngine(dataset_dir, full_state, cfg)
    full_result = full_engine.run()
    full_report_path = demo_root / "full_report.json"
    atomic_write_bytes(full_report_path, full_result.report_bytes)

    command = [
        sys.executable,
        "-m",
        "voice_state.cli",
        "--workdir",
        str(workdir),
        "--json",
        "run",
        "--dataset",
        str(dataset_dir),
        "--state",
        str(resumed_state),
        "--crash-after-stage",
        stop_after,
        "--max-dialogs",
        str(cfg.max_dialogs_per_stage),
        "--max-resident-bytes",
        str(cfg.max_resident_feature_bytes),
        "--max-segment-bytes",
        str(cfg.max_segment_memory_bytes),
        "--batch-size",
        str(cfg.batch_size),
        "--frame-ms",
        str(cfg.frame_ms),
        "--hop-ms",
        str(cfg.hop_ms),
    ]
    completed = subprocess.run(
        command,
        cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 75:
        raise BusinessError(
            E_INCONSISTENT,
            "Demonstration child process did not terminate at the requested stage",
            {
                "expected_returncode": 75,
                "actual_returncode": completed.returncode,
                "stdout": completed.stdout[-2000:],
                "stderr": completed.stderr[-2000:],
            },
        )

    resumed_engine = AnalysisEngine(dataset_dir, resumed_state, cfg)
    crash_status = resumed_engine.status()
    expected_last = stop_after
    if crash_status.get("last_committed_stage") != expected_last:
        raise BusinessError(
            E_INCONSISTENT,
            "Crash store does not end at the expected committed stage",
            {
                "expected_last_committed_stage": expected_last,
                "actual": crash_status.get("last_committed_stage"),
            },
        )
    recovered_result = resumed_engine.run()
    recovered_report_path = demo_root / "recovered_report.json"
    atomic_write_bytes(recovered_report_path, recovered_result.report_bytes)
    if full_result.report_bytes != recovered_result.report_bytes:
        raise BusinessError(
            E_INCONSISTENT,
            "Recovered report is not byte-identical to the uninterrupted report",
            {
                "full_report_hash": full_result.report_hash,
                "recovered_report_hash": recovered_result.report_hash,
                "full_bytes": len(full_result.report_bytes),
                "recovered_bytes": len(recovered_result.report_bytes),
            },
        )
    index = STAGE_ORDER.index(stop_after)
    expected_reused = list(STAGE_ORDER[: index + 1])
    expected_computed = list(STAGE_ORDER[index + 1 :])
    if recovered_result.reused_stages != expected_reused:
        raise BusinessError(
            E_INCONSISTENT,
            "Recovery did not reuse the committed snapshot prefix exactly",
            {
                "expected_reused_stages": expected_reused,
                "actual_reused_stages": recovered_result.reused_stages,
            },
        )
    if recovered_result.computed_stages != expected_computed:
        raise BusinessError(
            E_INCONSISTENT,
            "Recovery computed an unexpected stage set",
            {
                "expected_computed_stages": expected_computed,
                "actual_computed_stages": recovered_result.computed_stages,
            },
        )
    selfcheck = run_selfcheck(
        resumed_engine.store,
        expected_dialog_ids=[item.dialog_id for item in resumed_engine.dataset.dialogs],
        full_report_bytes=full_result.report_bytes,
        recovered_report_bytes=recovered_result.report_bytes,
    )
    return {
        "ok": True,
        "demo_root": str(demo_root),
        "stop_after_stage": stop_after,
        "crash_exit_code": completed.returncode,
        "full_report_hash": full_result.report_hash,
        "recovered_report_hash": recovered_result.report_hash,
        "report_bytes_identical": True,
        "reused_stages": recovered_result.reused_stages,
        "computed_stages": recovered_result.computed_stages,
        "selfcheck_checksum": selfcheck["checksum"],
        "full_state": str(full_state),
        "resume_state": str(resumed_state),
    }

