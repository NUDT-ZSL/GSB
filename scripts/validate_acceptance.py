#!/usr/bin/env python3
"""Validate the generated demo report and deterministic scale-selection probes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from voice_dialog.adaptive import select_strategy
from voice_dialog.config import load_config
from voice_dialog.reporting import stable_report_view
from voice_dialog.util import atomic_write_json


def _check(checks: list[dict[str, Any]], name: str, condition: bool, detail: Any) -> None:
    checks.append({"name": name, "passed": bool(condition), "detail": detail})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-report", required=True, type=Path)
    parser.add_argument("--incremental-report", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    full = json.loads(args.full_report.read_text(encoding="utf-8"))
    incremental = json.loads(args.incremental_report.read_text(encoding="utf-8"))
    config = load_config(args.config)
    checks: list[dict[str, Any]] = []

    _check(checks, "both_runs_completed", full["status"] == "completed" and incremental["status"] == "completed", {
        "full": full["status"], "incremental": incremental["status"]
    })
    stable_equal = stable_report_view(full) == stable_report_view(incremental)
    _check(checks, "stable_reports_equal", stable_equal, {
        "full_fingerprint": full["report_fingerprint"],
        "incremental_fingerprint": incremental["report_fingerprint"],
    })
    _check(checks, "report_fingerprints_equal", full["report_fingerprint"] == incremental["report_fingerprint"],
           {"full": full["report_fingerprint"], "incremental": incremental["report_fingerprint"]})

    segment_names = [segment["filename"] for segment in full["segments"]]
    ordered_names = sorted(segment_names, key=lambda value: (value.lower(), value))
    _check(checks, "segment_order_stable", segment_names == ordered_names, {"count": len(segment_names)})
    frame_order_ok = all(
        [frame["index"] for frame in segment["frame_features"]] == list(range(segment["frame_count"]))
        for segment in full["segments"]
    )
    _check(checks, "frame_order_stable", frame_order_ok, {"frame_count_total": full["diagnostics"]["frame_count_total"]})
    dialog_order = [(turn["order"], turn["turn_id"]) for turn in full["dialog_turns"]]
    _check(checks, "dialog_order_stable", dialog_order == sorted(dialog_order), {"turn_count": len(dialog_order)})
    failure_keys = [
        (item["phase"], item["filename"], item["turn_id"], item["code"], item["injection_id"])
        for item in full["failures"]
    ]
    phase_order = {"scan": 0, "audio": 1, "features": 2, "scores": 3, "fusion": 4, "internal": 5}
    expected_failure_keys = sorted(
        failure_keys,
        key=lambda item: (phase_order.get(item[0], 99), item[1].lower(), item[1], item[2], item[3], item[4]),
    )
    _check(checks, "failure_order_stable", failure_keys == expected_failure_keys, {"failure_count": len(failure_keys)})

    hit_ids = [item["id"] for item in full["injections"]["hits"]]
    _check(checks, "all_injections_hit", hit_ids == ["INJ-001", "INJ-002", "INJ-003", "INJ-004", "INJ-005"], hit_ids)
    failure_codes = sorted({item["code"] for item in full["failures"]})
    expected_codes = [
        "E_AUDIO_FORMAT", "E_INVALID_GRANULARITY", "E_MISSING_PREDECESSOR",
        "E_NONFINITE_SAMPLE", "E_ZERO_DURATION",
    ]
    _check(checks, "expected_business_codes", failure_codes == expected_codes, failure_codes)

    cache = incremental["cache"]
    cache_hit = all(cache[stage]["misses"] == 0 for stage in ("features", "scores", "fusion"))
    _check(checks, "unchanged_incremental_run_reuses_all_cache", cache_hit, {
        stage: {"hits": cache[stage]["hits"], "misses": cache[stage]["misses"]}
        for stage in ("features", "scores", "fusion")
    })

    commit_fields_ok = True
    committed = full["snapshots"]["committed"]
    for stage in ("features", "scores", "fusion"):
        snapshot = committed.get(stage, {})
        commit_fields_ok = commit_fields_ok and all(
            key in snapshot for key in (
                "stage", "config_hash", "injection_hash", "processed_cursor",
                "artifact_digest", "snapshot_version", "status",
            )
        )
    _check(checks, "snapshot_fields_complete", commit_fields_ok, {
        "versions": full["snapshots"]["versions"], "stages": sorted(committed)
    })

    probes = []
    probe_metrics = [
        ("small", 30, 6, 0.8),
        ("medium", 300, 40, 0.8),
        ("large", 2000, 100, 0.8),
    ]
    for label, segments, turns, duration in probe_metrics:
        kwargs = dict(
            segment_count=segments,
            valid_durations=[duration] * segments,
            turn_count=turns,
            granularity=config["emotion_granularity"],
            config=config,
        )
        first = select_strategy(**kwargs)
        second = select_strategy(**kwargs)
        probes.append({
            "label": label,
            "segments": segments,
            "turns": turns,
            "tier": first["metrics"]["tier"],
            "volume_units": first["metrics"]["volume_units"],
            "strategy_id": first["selected"]["strategy_id"],
            "classifier": first["effective"]["classifier"],
            "fusion_window": first["effective"]["fusion_window"],
            "feature_count": first["effective"]["feature_count"],
            "deterministic": first == second,
        })
    _check(checks, "scale_probes_deterministic", all(probe["deterministic"] for probe in probes), probes)
    _check(checks, "scale_probe_tiers", [probe["tier"] for probe in probes] == ["small", "medium", "large"],
           [probe["tier"] for probe in probes])
    _check(checks, "scale_probe_strategies_change", len({probe["strategy_id"] for probe in probes}) == 3,
           [probe["strategy_id"] for probe in probes])

    diagnostics = full["diagnostics"]
    summary = {
        "all_checks_passed": all(check["passed"] for check in checks),
        "checks": checks,
        "dataset": {
            "segments": diagnostics["segment_count"],
            "valid_audio": diagnostics["valid_audio_count"],
            "feature_ready": diagnostics["feature_ready_count"],
            "scored": diagnostics["scored_count"],
            "turns": diagnostics["turn_count"],
            "frames": diagnostics["frame_count_total"],
            "avg_duration_sec": full["scale_decision"]["metrics"]["average_duration_sec"],
        },
        "scale_decision": {
            "tier": diagnostics["scale_tier"],
            "strategy_id": full["scale_decision"]["selected"]["strategy_id"],
            "classifier": diagnostics["effective_classifier"],
            "feature_count": diagnostics["effective_feature_count"],
            "fusion_window": diagnostics["effective_fusion_window"],
            "reason": full["scale_decision"]["reason"],
        },
        "emotion_accuracy": diagnostics["accuracy"],
        "failures": full["failures"],
        "injection_hits": full["injections"]["hits"],
        "incremental_cache": cache,
        "scale_probes": probes,
        "snapshot_versions": full["snapshots"]["versions"],
    }
    atomic_write_json(args.out, summary)
    print(json.dumps({
        "all_checks_passed": summary["all_checks_passed"],
        "failed_checks": [item["name"] for item in checks if not item["passed"]],
        "out": str(args.out),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if summary["all_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
