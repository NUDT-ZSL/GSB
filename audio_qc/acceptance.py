"""Local acceptance orchestration for crash recovery and equivalence."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .codes import BusinessCode
from .detect import detect_drift, replay_report
from .dsp import estimate_envelope_oneshot, read_pcm_s16
from .hashing import canonical_hash, sha256_bytes
from .pipeline import PipelineRequest, run_pipeline
from .samples import DEFAULT_SEED, write_sample_bundle
from .thresholds import read_thresholds
from .verify import verify_migration_gates, verify_serial_vs_oneshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _event_core(report_or_drift: dict[str, Any], events_key: str = "events") -> list[tuple[Any, ...]]:
    return [
        (
            event["frame_index"],
            event["start_sample"],
            event["end_sample"],
            event["business_code"],
            event["precomputed_hash"],
        )
        for event in report_or_drift[events_key]
    ]


def run_acceptance(workdir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    workdir = Path(workdir)
    if not workdir.is_absolute():
        workdir = Path.cwd() / workdir
    sample_dir = workdir / "samples"
    baseline_state = workdir / "baseline-state"
    crash_state = workdir / "crash-state"
    baseline_report_path = workdir / "baseline-report.json"
    recovered_report_path = workdir / "recovered-report.json"
    if baseline_state.exists():
        shutil.rmtree(baseline_state)
    if crash_state.exists():
        shutil.rmtree(crash_state)

    paths = write_sample_bundle(sample_dir, seed)
    pcm, raw = read_pcm_s16(paths["pcm"])
    pcm_hash = sha256_bytes(raw)
    v1 = read_thresholds(paths["v1"])
    v2 = read_thresholds(paths["v2"])

    baseline = run_pipeline(
        PipelineRequest(
            pcm=pcm,
            pcm_hash=pcm_hash,
            threshold_doc=v1,
            state_dir=baseline_state,
            report_path=baseline_report_path,
            workers=1,
            run_self_check=True,
            owner="acceptance-baseline",
        )
    )

    crash_command = [
        sys.executable,
        "-m",
        "audio_qc.cli",
        "run",
        "--sample-dir",
        str(sample_dir),
        "--threshold",
        "v1",
        "--state-dir",
        str(crash_state),
        "--workers",
        "1",
        "--crash-after",
        "3",
    ]
    crashed = subprocess.run(
        crash_command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    current_after_crash = crash_state / "current.json"
    current_existed_after_hard_exit = current_after_crash.exists()
    cache_entries_after_crash = list((crash_state / "cache" / "entries").glob("*.json"))

    recovered = run_pipeline(
        PipelineRequest(
            pcm=pcm,
            pcm_hash=pcm_hash,
            threshold_doc=v1,
            state_dir=crash_state,
            report_path=recovered_report_path,
            workers=1,
            owner="acceptance-recovery",
        )
    )

    oneshot_blocks = estimate_envelope_oneshot(pcm, v1["frame_length"], v1["frame_hop"])
    oneshot_drift = detect_drift(oneshot_blocks, v1)
    serial_oneshot_check = verify_serial_vs_oneshot(pcm, v1)
    migration_v1 = verify_migration_gates(_blocks_for_migration(pcm, v1), v1)
    migration_v2 = verify_migration_gates(_blocks_for_migration(pcm, v2), v2)
    replay = replay_report(recovered["report"])

    baseline_core = _event_core(baseline["report"])
    recovered_core = _event_core(recovered["report"])
    oneshot_core = _event_core(oneshot_drift)
    equivalence = {
        "baseline_vs_recovered_events_equal": baseline_core == recovered_core,
        "recovered_vs_oneshot_events_equal": recovered_core == oneshot_core,
        "baseline_envelope_hash": baseline["report"]["envelope_hash"],
        "recovered_envelope_hash": recovered["report"]["envelope_hash"],
        "envelope_hashes_equal": baseline["report"]["envelope_hash"] == recovered["report"]["envelope_hash"],
        "baseline_drift_hash": baseline["report"]["drift_hash"],
        "recovered_drift_hash": recovered["report"]["drift_hash"],
        "drift_hashes_equal": baseline["report"]["drift_hash"] == recovered["report"]["drift_hash"],
        "event_count": len(recovered_core),
    }

    crash_trace = recovered["trace"]
    result = {
        "ok": bool(
            crashed.returncode == 42
            and not current_existed_after_hard_exit
            and len(cache_entries_after_crash) == 3
            and equivalence["baseline_vs_recovered_events_equal"]
            and equivalence["recovered_vs_oneshot_events_equal"]
            and serial_oneshot_check["ok"]
            and migration_v1["ok"]
            and migration_v2["ok"]
            and replay["ok"]
        ),
        "code": BusinessCode.VERIFY_OK,
        "sample_dir": str(sample_dir),
        "crash": {
            "command": " ".join(crash_command),
            "exit_code": crashed.returncode,
            "expected_exit_code": 42,
            "current_snapshot_exists_immediately_after_hard_exit": current_existed_after_hard_exit,
            "durable_chunk_entries_after_crash": len(cache_entries_after_crash),
            "stderr_tail": crashed.stderr[-500:],
        },
        "recovery": {
            "envelope_reused_cache_chunks": crash_trace["envelope_reused_cache_chunks"],
            "envelope_recomputed_count": len(crash_trace["envelope_recomputed_chunks"]),
            "envelope_loaded_snapshot": crash_trace["envelope_loaded_snapshot"],
            "alignment_loaded_snapshot": crash_trace["alignment_loaded_snapshot"],
            "drift_loaded_snapshot": crash_trace["drift_loaded_snapshot"],
        },
        "equivalence": equivalence,
        "self_check": {
            "code": serial_oneshot_check["code"],
            "block_count": serial_oneshot_check.get("block_count"),
            "event_count": serial_oneshot_check.get("event_count"),
            "event_codes": serial_oneshot_check.get("event_codes"),
            "first_mismatch": serial_oneshot_check.get("first_mismatch"),
        },
        "migration": {
            "v1_to_v2_gate_code": migration_v1["code"],
            "v1_compared_windows": migration_v1["equivalence"]["compared_windows"],
            "v2_to_v1_gate_code": migration_v2["code"],
            "v2_compared_windows": migration_v2["equivalence"]["compared_windows"],
        },
        "replay": {"replayed_events": replay["replayed_events"], "ok": True},
        "first_drift_events": [
            {
                "frame_index": event["frame_index"],
                "sample_interval": event["sample_interval"],
                "segment_id": event["segment_id"],
                "business_code": event["business_code"],
                "dominant_subband": event["dominant_subband"],
                "threshold_version": event["threshold_version"],
                "precomputed_hash": event["precomputed_hash"][:16],
            }
            for event in recovered["report"]["events"][:8]
        ],
        "business_codes": {
            "success": BusinessCode.VERIFY_OK,
            "unsupported_version": BusinessCode.E_VERSION_UNSUPPORTED,
            "corrupt_format": BusinessCode.E_FORMAT_CORRUPT,
            "timeline_overlap": BusinessCode.E_TIMELINE_OVERLAP,
            "stage_busy": BusinessCode.E_STAGE_BUSY,
            "memory_budget": BusinessCode.E_MEMORY_BUDGET,
            "resource_limit": BusinessCode.E_RESOURCE_LIMIT,
            "inconsistent": BusinessCode.E_INCONSISTENT,
        },
    }
    if not result["ok"]:
        result["code"] = BusinessCode.E_INCONSISTENT
    summary_path = workdir / "acceptance_summary.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["summary_path"] = str(summary_path)
    return result


def _blocks_for_migration(pcm: Any, doc: dict[str, Any]) -> list[dict[str, Any]]:
    from .dsp import estimate_envelope_serial

    return estimate_envelope_serial(pcm, doc["frame_length"], doc["frame_hop"])
