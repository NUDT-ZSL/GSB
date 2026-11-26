from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .audio import extract_all_features
from .incremental import IncrementalManager
from .pipeline import AuditPipeline
from .query import QueryIndex
from .segmentation import readjudicate_with_lineage, split_segment
from .identity import IdentityAdjudicator, build_speech_segments
from .sample_generator import generate_sample
from .verification import verify_deterministic_foundations


def _ok(report: dict[str, Any]) -> bool:
    return bool(report.get("ok"))


def run_deep_verification(workspace: str | Path) -> dict[str, Any]:
    pipeline = AuditPipeline(workspace)
    base = pipeline.run_full(revision_id="deep-base")
    pcm, _manifest = pipeline.ensure_sample()
    manager = IncrementalManager(pipeline)

    foundation = verify_deterministic_foundations(
        pcm, base["v1_rules"], base["turn_points"], pipeline.config
    )
    truth = manager.apply_truth_correction("SEG-0005", "SPK_B", "deep-truth")
    rules = manager.promote_rules(
        "v2.1.0-deep",
        {"rapid_switch_ms": 800, "silence_long_ms": 1800},
        "deep-rules",
    )

    revised_pcm = pcm.copy()
    revision_start = 15_000 * pipeline.config.samples_per_ms
    revised_pcm[revision_start:] = np.clip(
        np.rint(revised_pcm[revision_start:].astype(np.float64) * 0.82),
        -32768,
        32767,
    ).astype(np.int16)
    audio = manager.apply_audio_revision(
        revised_pcm, "deep-audio", "deterministic 0.82 gain after 15s"
    )

    result = pipeline.load_last_result()
    query_index = QueryIndex(result, pipeline.config)
    first_query = query_index.at_ms(12120)
    second_query = query_index.at_ms(12120)
    query_repeat_ok = first_query == second_query

    versions_before = {
        stage: pipeline.snapshots.current_version(stage)
        for stage in (
            "frame_features",
            "turn_detection",
            "anomaly_classification",
            "identity_adjudication",
            "ledger_write",
            "audit_report",
        )
    }
    pipeline.snapshots.write_fake_uncommitted(
        "frame_features", {"stage": "frame_features", "version": 999, "committed": False}
    )
    recovered = pipeline.snapshots.recover()
    versions_after = {
        stage: pipeline.snapshots.current_version(stage) for stage in versions_before
    }
    recovery_ok = (
        len(recovered["discarded_intermediate_files"]) >= 1
        and versions_before == versions_after
    )

    sample = generate_sample(pipeline.config)
    features = extract_all_features(sample.pcm_int16, pipeline.config)
    from .detector import detect_turns

    detection = detect_turns(features, pipeline.config)
    segments = build_speech_segments(
        detection, sample.pcm_int16, features, pipeline.config
    )
    adjudicator = IdentityAdjudicator(pipeline.config)
    for segment in segments:
        adjudicator.assign(segment)
    original_labels = {
        key: value["speaker_id"] for key, value in adjudicator.assignments.items()
    }
    revised_segments = split_segment(
        segments, "SEG-0001", 1800, sample.pcm_int16, features, pipeline.config
    )
    segmentation_result = readjudicate_with_lineage(
        revised_segments, adjudicator, pipeline.config
    )
    new_assignments = segmentation_result["assignments"]
    segmentation_ok = (
        new_assignments["SEG-0001__L1800"]["speaker_id"]
        == new_assignments["SEG-0001__R1800"]["speaker_id"]
        == original_labels["SEG-0001"]
        and all(
            original_labels[segment_id] == new_assignments[segment_id]["speaker_id"]
            for segment_id in original_labels
            if segment_id != "SEG-0001"
        )
    )

    steps = {
        "foundation": {
            "ok": foundation["ok"],
            "chunked_ok": foundation["chunked_detection"]["ok"],
            "float_fixed_ok": foundation["float_fixed_parity"]["ok"],
            "rule_migration_ok": foundation["rule_migration"]["ok"],
        },
        "truth_correction": {
            "ok": truth["ok"],
            "recomputed_segments": truth["recomputed"]["segments"],
            "recomputed_ledger_rows": truth["recomputed"]["ledger_rows"],
            "reused_ledger_row_count": len(truth["reused"]["ledger_rows"]),
        },
        "rule_promotion": {
            "ok": rules["ok"],
            "rule_version": rules["rule_version"],
            "recomputed_ledger_row_count": len(rules["recomputed"]["ledger_rows"]),
        },
        "audio_revision": {
            "ok": audio["ok"],
            "changed_ms_range": audio["invalidation"]["changed_ms_range"],
            "recomputed_ledger_rows": audio["recomputed"]["ledger_rows"],
            "reused_ledger_rows": audio["reused"]["ledger_rows"],
        },
        "query_repeat": {"ok": query_repeat_ok},
        "snapshot_recovery": {
            "ok": recovery_ok,
            "discarded": recovered["discarded_intermediate_files"],
            "versions": versions_after,
        },
        "segmentation_stability": {
            "ok": segmentation_ok,
            "recomputed": segmentation_result["recomputed"],
        },
        "final_self_check": {
            "ok": result["self_check"]["ok"],
            "error_codes": result["self_check"].get("error_codes", []),
        },
    }
    overall_ok = all(
        step.get("ok", False)
        for key, step in steps.items()
        if isinstance(step, dict) and key != "final_self_check"
    ) and steps["final_self_check"]["ok"]
    return {"ok": overall_ok, "workspace": str(pipeline.workspace), "steps": steps}
