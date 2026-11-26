"""Incremental invalidation and suffix recomputation scenarios."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Sequence

from .anomalies import detect_turn_anomalies
from .cache import (
    ContentCache,
    plan_generation_invalidation,
    plan_truth_invalidation,
)
from .canonical import atomic_write_json, read_json
from .emotions import advance_states
from .ledger import read_ledger, write_v2_ledger
from .migrate import downgrade_v2_to_v1, roundtrip_check
from .pipeline import read_jsonl
from .samplegen import canonical_emotion_labels, write_jsonl


def run_truth_correction(
    sample_dir: str | Path,
    output_dir: str | Path,
    frame_index: int = 650,
    updates: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    from .pipeline import load_sample_bundle

    bundle = load_sample_bundle(sample_dir)
    config = bundle["config"]
    truth = bundle["truth"]
    features = read_jsonl(Path(output_dir) / "frame_features.jsonl")
    baseline_states = read_jsonl(Path(output_dir) / "emotion_states.jsonl")
    baseline_anomalies = read_json(Path(output_dir) / "anomalies.json")
    frame_count = len(truth)

    new_truth = copy.deepcopy(truth)
    patch = dict(updates or {"speaker_id": 1})
    patch.setdefault("is_speech", int(patch.get("speaker_id", 0)) > 0)
    if "speaker_id" in patch and int(patch["speaker_id"]) == 0:
        patch["is_speech"] = False
    new_truth[int(frame_index)].update(patch)

    plan = plan_truth_invalidation(truth, new_truth, frame_count)
    first = plan["earliest_changed_frame"] or 0
    initial_state = "neutral"
    if first > 0:
        initial_state = str(baseline_states[first - 1]["state"])

    suffix_result = advance_states(
        features,
        new_truth,
        config,
        initial_state=initial_state,
        start_index=first,
        end_index=frame_count,
    )
    merged_states = baseline_states[:first] + suffix_result["records"]
    new_anomalies = detect_turn_anomalies(new_truth, features, config)

    reusable_events = [
        event
        for event in baseline_anomalies.get("events", [])
        if int(event["end_frame"]) <= first
    ]
    affected_new_events = [
        event
        for event in new_anomalies.get("events", [])
        if int(event["end_frame"]) > first
    ]
    merged_events = sorted(
        reusable_events + affected_new_events,
        key=lambda event: (int(event["start_frame"]), event["event_id"]),
    )
    merged_anomalies = {
        "status": "anomalies_detected" if merged_events else "no_anomaly",
        "empty": not merged_events,
        "marker": "ANOMALY_LIST_EMPTY" if not merged_events else "ANOMALY_LIST_NONEMPTY",
        "anomaly_count": len(merged_events),
        "events": merged_events,
        "incremental_note": "prefix events committed and reused; intersecting suffix events recomputed",
    }

    incremental_dir = Path(output_dir) / "incremental" / "truth_correction"
    incremental_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(incremental_dir / "corrected_truth.jsonl", new_truth)
    write_jsonl(incremental_dir / "recomputed_state_suffix.jsonl", suffix_result["records"])
    write_jsonl(incremental_dir / "merged_states.jsonl", merged_states)
    atomic_write_json(incremental_dir / "merged_anomalies.json", merged_anomalies)

    cache = ContentCache(Path(output_dir) / "cache")
    new_hashes = []
    for record in suffix_result["records"]:
        content_hash = cache.put(record)
        new_hashes.append(content_hash)

    report = {
        "scenario": "truth_frame_correction",
        "corrected_frame": int(frame_index),
        "patch": patch,
        "plan": plan,
        "feature_recompute_ranges": [],
        "feature_reused_ranges": [[0, frame_count]],
        "state_recompute_ranges": plan["recompute_ranges"],
        "state_reused_ranges": plan["reused_ranges"],
        "recomputed_state_frames": frame_count - first,
        "reused_state_frames": first,
        "reused_anomaly_event_count": len(reusable_events),
        "recomputed_anomaly_event_count": len(affected_new_events),
        "new_state_content_hashes": new_hashes[:12],
        "final_state_before": baseline_states[-1]["state"],
        "final_state_after": merged_states[-1]["state"],
        "output_dir": str(incremental_dir),
    }
    atomic_write_json(incremental_dir / "incremental_report.json", report)
    atomic_write_json(Path(output_dir) / "incremental_truth_report.json", report)
    return report


def run_generation_update(
    sample_dir: str | Path,
    output_dir: str | Path,
    *,
    revised_start: int = 760,
    new_generation: int = 2,
    revision: int = 1,
) -> Dict[str, Any]:
    from .pipeline import load_sample_bundle

    bundle = load_sample_bundle(sample_dir)
    config = bundle["config"]
    truth = bundle["truth"]
    frame_count = len(truth)
    revised_start = max(0, min(revised_start, frame_count - 1))

    old_ledger = read_ledger(bundle["v2_path"])
    labels = canonical_emotion_labels(truth, config)
    new_labels = list(labels)
    for index in range(revised_start, frame_count):
        new_labels[index] = "angry" if index % 2 == 0 else "happy"
    generation_values = [1] * revised_start + [new_generation] * (
        frame_count - revised_start
    )
    revision_values = [0] * revised_start + [revision] * (
        frame_count - revised_start
    )

    incoming_dir = Path(output_dir) / "incremental" / "generation_update" / "incoming_v2"
    write_v2_ledger(
        incoming_dir,
        truth,
        new_labels,
        config,
        generation=new_generation,
        revision=revision,
        generation_values=generation_values,
        revision_values=revision_values,
    )
    new_ledger = read_ledger(incoming_dir)
    plan = plan_generation_invalidation(
        old_ledger["records"],
        new_ledger["records"],
        frame_count,
        new_generation=new_generation,
        old_generation=int(config["default_generation"]),
    )
    first = plan["earliest_changed_frame"] or 0

    old_prefix = old_ledger["records"][:first]
    new_suffix = new_ledger["records"][first:]
    merged = old_prefix + new_suffix
    merged_path = Path(output_dir) / "incremental" / "generation_update" / "merged_ledger.jsonl"
    write_jsonl(merged_path, [{k: v for k, v in r.items() if k not in {"raw"}} for r in merged])

    downgrade = downgrade_v2_to_v1(new_ledger, config)
    roundtrip = roundtrip_check(new_ledger, truth, config)

    cache = ContentCache(Path(output_dir) / "cache")
    cached_suffix = []
    for record in new_suffix:
        slim = {k: v for k, v in record.items() if k not in {"raw"}}
        content_hash = cache.put(slim)
        cached_suffix.append(content_hash)

    report = {
        "scenario": "v2_higher_generation",
        "incoming_ledger": str(incoming_dir),
        "plan": plan,
        "ledger_recompute_ranges": plan["recompute_ranges"],
        "ledger_reused_ranges": plan["reused_ranges"],
        "recomputed_ledger_entries": frame_count - first,
        "reused_ledger_entries": first,
        "discard_count": downgrade["discard_count"],
        "discard_records_preview": downgrade["discard_records"][:10],
        "roundtrip_equivalent": roundtrip["roundtrip_equivalence"]["equivalent"],
        "metadata_restorable": roundtrip["metadata_restorable"],
        "suffix_content_hashes": cached_suffix[:12],
        "merged_ledger": str(merged_path),
    }
    report_path = Path(output_dir) / "incremental_generation_report.json"
    atomic_write_json(report_path, report)
    atomic_write_json(
        Path(output_dir) / "incremental" / "generation_update" / "generation_report.json",
        report,
    )
    return report
