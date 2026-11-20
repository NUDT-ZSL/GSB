#!/usr/bin/env python3
"""End-to-end demo: full run, truth correction, incremental run and replay."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_emotion import (  # noqa: E402
    BusinessError,
    FeatureConfig,
    PipelineConfig,
    ResourceLimits,
    VoiceEmotionPipeline,
    build_demo_dataset,
    load_dataset,
)
from voice_emotion.dataset import read_jsonl, write_jsonl  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workdir",
        help="keep all generated files in this directory instead of a temporary directory",
    )
    parser.add_argument("--dialogs", type=int, default=3)
    parser.add_argument("--duration-ms", type=int, default=2_400)
    args = parser.parse_args()
    temporary = args.workdir is None
    workdir = Path(tempfile.mkdtemp(prefix="voice-emotion-demo-")) if temporary else Path(args.workdir)
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        dataset = build_demo_dataset(
            workdir / "samples",
            dialog_count=args.dialogs,
            duration_ms=args.duration_ms,
        )
        config = PipelineConfig(
            resources=ResourceLimits(
                max_frames_per_analysis=100_000,
                max_cache_entries=10_000,
                max_feature_bytes=64 * 1024 * 1024,
            )
        )
        pipeline = VoiceEmotionPipeline(workdir / "cache", config)
        initial = pipeline.analyze_dataset(dataset)
        initial_dialog = initial.data["dialogs"][0]
        target_segment = max(
            initial_dialog["segments"],
            key=lambda item: int(item["frame_count"]),
        )

        truth_path = dataset.dialogs[0].truth_path
        truth_rows = read_jsonl(truth_path)
        for index in range(
            target_segment["start_frame"], target_segment["end_frame"]
        ):
            truth_rows[index]["valence"] = round(
                min(1.0, float(truth_rows[index]["valence"]) + 0.2), 6
            )
        write_jsonl(truth_path, truth_rows)
        updated_dataset = load_dataset(
            dataset.manifest_path,
            dataset.enrollment_path,
        )
        incremental = pipeline.analyze_dataset(
            updated_dataset,
            previous_run=initial,
        )

        fresh_pipeline = VoiceEmotionPipeline(workdir / "cache-full", config)
        full = fresh_pipeline.analyze_dataset(updated_dataset)
        report = incremental.data["report"]
        old_dialog_record = initial_dialog["lineage"]["dialog_record_id"]
        old_replay = pipeline.trace_score(old_dialog_record)
        new_dialog_record = incremental.data["dialogs"][0]["lineage"]["dialog_record_id"]
        new_replay = pipeline.trace_score(new_dialog_record)

        try:
            VoiceEmotionPipeline(
                workdir / "cache-error",
                PipelineConfig(
                    feature=FeatureConfig(frame_shift_ms=30.0)
                ),
            ).analyze_dataset(updated_dataset)
        except BusinessError as exc:
            expected_error = exc.to_dict()
        else:
            expected_error = {"code": "NONE"}

        summary = {
            "status": "ok",
            "workdir": str(workdir),
            "initial_run_id": initial.run_id,
            "incremental_run_id": incremental.run_id,
            "incremental_reused_nodes": len(report["reused_nodes"]),
            "incremental_recomputed_nodes": len(report["recomputed_nodes"]),
            "recomputed_in_affected_dialog": [
                {"node": item["node"], "segment_id": item["segment_id"]}
                for item in report["recomputed_nodes"]
                if item["dialog_id"] == dataset.dialogs[0].dialog_id
            ],
            "affected_frames": report["affected_frames"][dataset.dialogs[0].dialog_id],
            "affected_segments": report["affected_segments"][
                dataset.dialogs[0].dialog_id
            ],
            "incremental_equals_full_recompute": (
                incremental.result_fingerprint == full.result_fingerprint
            ),
            "old_score_replay_matches": (
                old_replay["replayed"] == initial_dialog["dialog"]["scores"]
            ),
            "new_score_replay_matches": (
                new_replay["replayed"]
                == incremental.data["dialogs"][0]["dialog"]["scores"]
            ),
            "old_trace": {
                "record_id": old_dialog_record,
                "config_version": old_replay["config_version"],
                "input_versions": old_replay["input_versions"],
                "node_path_length": len(old_replay["node_path"]),
            },
            "expected_validation_error": expected_error,
            "self_check": pipeline.self_check(incremental),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        if not summary["incremental_equals_full_recompute"]:
            return 2
        if not (
            summary["old_score_replay_matches"] and summary["new_score_replay_matches"]
        ):
            return 3
        return 0
    finally:
        if temporary:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
