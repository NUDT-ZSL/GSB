"""Reproducible local acceptance run; prints a compact machine-readable summary."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from voice_state.canonical import atomic_write_bytes
from voice_state.demo import run_demo
from voice_state.engine import AnalysisEngine
from voice_state.selfcheck import run_selfcheck
from voice_state.synth import append_registry_sample, generate_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="artifacts/validation")
    parser.add_argument("--dialogs", type=int, default=4)
    parser.add_argument("--speakers", type=int, default=3)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    dataset_dir = out / "dataset"
    generate_dataset(
        dataset_dir,
        dialog_count=args.dialogs,
        speaker_count=args.speakers,
        seed=20261120,
        force=True,
    )
    full_engine = AnalysisEngine(dataset_dir, out / "full_state")
    full = full_engine.run()
    atomic_write_bytes(out / "full_report.json", full.report_bytes)
    full_check = run_selfcheck(
        full_engine.store,
        expected_dialog_ids=[item.dialog_id for item in full_engine.dataset.dialogs],
        full_report_bytes=full.report_bytes,
        recovered_report_bytes=full.report_bytes,
    )

    demo = run_demo(
        out / "demo_workspace",
        stop_after="segmentation",
        dialog_count=args.dialogs,
        speaker_count=args.speakers,
    )

    v1_status = full_engine.status()
    append_registry_sample(
        dataset_dir / "registrations.jsonl",
        speaker_id=0,
        clip_seed=20261120 + 900001,
        clip_ms=840,
    )
    evolved_engine = AnalysisEngine(dataset_dir, out / "full_state")
    evolved = evolved_engine.run()
    evolved_after = evolved_engine.status()
    atomic_write_bytes(out / "registry_v2_report.json", evolved.report_bytes)
    evolved_check = run_selfcheck(
        evolved_engine.store,
        expected_dialog_ids=[item.dialog_id for item in evolved_engine.dataset.dialogs],
    )
    version_vectors = [
        evolved_after["stages"][stage]["version"]
        for stage in ("framing", "segmentation", "regression", "aggregation")
    ]
    summary = {
        "ok": (
            demo["report_bytes_identical"]
            and full_check["ok"]
            and evolved_check["ok"]
            and evolved_after["registry_version"] == v1_status["registry_version"] + 1
            and all(version > v1_status["generation"] for version in version_vectors)
        ),
        "python": sys.version.split()[0],
        "numpy": __import__("numpy").__version__,
        "dataset": {
            "dialogs": args.dialogs,
            "speakers": args.speakers,
            "frame_ms": 20,
            "sample_rate": 16000,
        },
        "full_run": {
            "report_hash": full.report_hash,
            "snapshot_versions": {
                stage: full.status["stages"][stage]["version"]
                for stage in ("framing", "segmentation", "regression", "aggregation")
            },
            "selfcheck_checksum": full_check["checksum"],
        },
        "crash_recovery": demo,
        "registry_evolution": {
            "previous_registry_version": v1_status["registry_version"],
            "new_registry_version": evolved_after["registry_version"],
            "previous_generation": v1_status["generation"],
            "new_snapshot_versions": version_vectors,
            "epoch": evolved_after["epoch"],
            "archived_snapshot_prefixes": len(
                list((out / "full_state" / "archive").iterdir())
            ),
            "recomputed_stages": evolved.computed_stages,
            "selfcheck_checksum": evolved_check["checksum"],
            "report_hash": evolved.report_hash,
        },
    }
    atomic_write_bytes(
        out / "summary.json",
        json.dumps(summary, indent=2, sort_keys=True).encode() + b"\n",
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

