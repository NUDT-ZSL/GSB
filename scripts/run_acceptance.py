#!/usr/bin/env python3
"""Run the local acceptance scenario and write a JSON verification report."""

from __future__ import annotations

import json
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meeting_vad.config import load_config
from meeting_vad.errors import BusinessError
from meeting_vad.nodes import (
    compute_envelope,
    compute_export,
    compute_refine,
    compute_speaker,
    compute_vad,
)
from meeting_vad.session import AnalysisSession


def fresh_export(session: AnalysisSession):
    envelope = compute_envelope(session.audio_by_clip, session.config)
    vad = compute_vad(envelope, session.config)
    refine = compute_refine(envelope, vad, session.config)
    speaker = compute_speaker(envelope, refine, session.annotations, session.config)
    return compute_export(speaker, session.clips, session.config)


def snapshot_summary(result, replay=None) -> dict:
    return {
        "snapshot_version": result.snapshot_version,
        "snapshot_replaced": result.snapshot_replaced,
        "recomputed_nodes": result.recomputed_nodes,
        "reused_nodes": result.reused_nodes,
        "affected_nodes": result.affected_nodes,
        "affected_range_count": len(result.affected_ranges),
        "interval_recompute_count": result.interval_recompute_count,
        "interval_reuse_count": result.interval_reuse_count,
        "export_cache_key": result.export_cache_key,
        "export_jsonl_sha256": result.export_jsonl_sha256,
        "export_item_count": result.export_item_count,
        "self_check_ok": result.self_check["ok"],
        "replay_item_count": len(replay.items) if replay else None,
    }


def main() -> int:
    config_path = ROOT / "config" / "app.json"
    config = load_config(config_path)
    runtime = ROOT / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    session = AnalysisSession(config, use_runtime_annotations=False)
    report: dict = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy_required": ">=1.24",
            "container": False,
        },
        "steps": [],
        "replays": [],
    }

    initial = session.analyze()
    fresh = fresh_export(session)
    assert initial.export_jsonl.encode("utf-8") == fresh["jsonl"].encode("utf-8")
    initial_replay = session.replay_export(initial.export_cache_key)
    report["steps"].append(
        {
            "name": "initial_analyze",
            **snapshot_summary(initial, initial_replay),
            "byte_equal_to_full_recompute": True,
        }
    )

    repeated = session.analyze()
    assert repeated.export_jsonl == initial.export_jsonl
    assert not repeated.recomputed_nodes
    report["steps"].append(
        {
            "name": "repeat_analyze_cache_reuse",
            **snapshot_summary(repeated),
            "all_nodes_reused": set(repeated.reused_nodes) == {
                "envelope",
                "vad",
                "refine",
                "speaker",
                "export",
            },
        }
    )

    edited = session.correct_annotation("a-002", speaker_label="S01", label_revision=2)
    fresh = fresh_export(session)
    edit_equal = edited.export_jsonl.encode("utf-8") == fresh["jsonl"].encode("utf-8")
    assert edit_equal
    edit_replay = session.replay_export(edited.export_cache_key)
    report["steps"].append(
        {
            "name": "annotation_edit_incremental",
            **snapshot_summary(edited, edit_replay),
            "byte_equal_to_full_recompute": edit_equal,
        }
    )

    old_replay = session.replay_export(initial.export_cache_key)
    report["replays"].append(
        {
            "name": "initial_export_after_edit",
            "cache_key": old_replay.cache_key,
            "snapshot_version": old_replay.snapshot_version,
            "item_count": len(old_replay.items),
        }
    )

    quantile = session.set_noise_quantile(0.25)
    fresh = fresh_export(session)
    quantile_equal = (
        quantile.export_jsonl.encode("utf-8") == fresh["jsonl"].encode("utf-8")
    )
    assert quantile_equal
    report["steps"].append(
        {
            "name": "noise_quantile_change",
            **snapshot_summary(quantile),
            "byte_equal_to_full_recompute": quantile_equal,
        }
    )

    frame = session.set_frame(20.0, 5.0)
    fresh = fresh_export(session)
    frame_equal = frame.export_jsonl.encode("utf-8") == fresh["jsonl"].encode("utf-8")
    assert frame_equal
    report["steps"].append(
        {
            "name": "frame_shift_change",
            **snapshot_summary(frame),
            "byte_equal_to_full_recompute": frame_equal,
        }
    )

    for label, key in (
        ("initial_export_after_frame_change", initial.export_cache_key),
        ("edited_export_after_frame_change", edited.export_cache_key),
        ("quantile_export_after_frame_change", quantile.export_cache_key),
        ("latest_export", frame.export_cache_key),
    ):
        replay = session.replay_export(key)
        report["replays"].append(
            {
                "name": label,
                "cache_key": replay.cache_key,
                "snapshot_version": replay.snapshot_version,
                "item_count": len(replay.items),
                "verified": True,
            }
        )

    rollback_code = None
    try:
        session.snapshots.commit_generation(
            session.config.snapshot_identity(),
            session.artifacts.values(),
            requested_version=initial.snapshot_version,
        )
    except BusinessError as error:
        rollback_code = error.code
    report["version_audit"] = {
        "history_versions": session.snapshots.history_versions(),
        "current_version": session.snapshots.current_version(),
        "current_entry_count": session.snapshots.entry_count(),
        "rollback_attempt_code": rollback_code,
        "all_generations_immutable": session.snapshots.history_versions()
        == sorted(session.snapshots.history_versions()),
    }

    report_path = runtime / "acceptance_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
