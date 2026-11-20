#!/usr/bin/env python3
"""Run the complete incremental, lineage, and validation demonstration."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialog_emotion.config import (  # noqa: E402
    PipelineConfig,
    ResourceLimits,
    validate_dialog_rows,
    validate_registration_rows,
)
from dialog_emotion.cache import (  # noqa: E402
    ContentAddressedCache,
    make_cache_key,
)
from dialog_emotion.errors import BusinessError  # noqa: E402
from dialog_emotion.features import feature_product  # noqa: E402
from dialog_emotion.lineage import LineageStore  # noqa: E402
from dialog_emotion.pipeline import (  # noqa: E402
    AppendRegistration,
    CorrectTruth,
    IncrementalPipeline,
    SetFrameShift,
    SetSegmentThreshold,
    compare_runs,
)
from dialog_emotion.samples import generate_bundle, load_bundle, write_bundle  # noqa: E402
from dialog_emotion.scoring import validate_interval  # noqa: E402
from dialog_emotion.speaker import validate_assignment_labels  # noqa: E402
from dialog_emotion.segments import validate_segments  # noqa: E402
from dialog_emotion.truth import TruthStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--samples",
        default=str(ROOT / "data" / "samples"),
        help="sample directory; generated when absent",
    )
    parser.add_argument(
        "--work",
        default=str(ROOT / "artifacts" / "demo"),
        help="working directory for cache/config/lineage stores",
    )
    parser.add_argument(
        "--json",
        default=str(ROOT / "artifacts" / "demo-report.json"),
        help="JSON report output path",
    )
    args = parser.parse_args()
    report_path = Path(args.json)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = run_demo(Path(args.samples), Path(args.work))
        exit_code = 0
    except BusinessError as exc:
        report = {
            "status": "error",
            "business_codes": [exc.to_dict()],
            "error": exc.to_dict(),
        }
        exit_code = 2
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(report_path.resolve()),
                "business_codes": report.get("business_codes", []),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return exit_code


def run_demo(samples_dir: Path, work_dir: Path) -> dict[str, Any]:
    if not (samples_dir / "dialogs.jsonl").exists():
        write_bundle(generate_bundle(), samples_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    baseline_bundle = load_bundle(samples_dir)
    bundle = baseline_bundle.copy()
    engine = IncrementalPipeline(
        bundle,
        config=bundle.base_config,
        cache_dir=work_dir / "cache",
        config_snapshot_dir=work_dir / "config-snapshots",
        lineage_dir=work_dir / "lineage",
    )
    baseline = engine.run()
    if not baseline.report["self_check"]["passed"]:
        fail_self_check(baseline.report)

    original_segment_record = _first_record(
        baseline, kind="segment_score", dialog_id="dlg-002"
    )
    original_dialog_record = _first_record(
        baseline, kind="dialog_score", dialog_id="dlg-002"
    )
    original_segment_id = original_segment_record["record_id"]
    original_dialog_id = original_dialog_record["record_id"]
    original_segment_score = original_segment_record["score"]
    original_dialog_score = original_dialog_record["score"]

    events = [
        AppendRegistration("spk_a", 991, 850),
        CorrectTruth("dlg-002", valence=0.12, arousal=0.88, segment_index=1),
        SetFrameShift(15.0),
        SetSegmentThreshold(350.0),
    ]
    incremental_events: list[dict[str, Any]] = []
    final_result = baseline
    for event in events:
        final_result = engine.run([event])
        if not final_result.report["self_check"]["passed"]:
            fail_self_check(final_result.report)
        incremental_events.append(
            {
                "change": _change_to_dict(event),
                "affected_dialog_ids": final_result.report[
                    "affected_dialog_ids"
                ],
                "affected_frame_counts": {
                    dialog_id: len(frames)
                    for dialog_id, frames in final_result.report[
                        "affected_frames"
                    ].items()
                },
                "affected_segments": final_result.report["affected_segments"],
                "reused_nodes": final_result.report["reused_nodes"],
                "recomputed_nodes": final_result.report["recomputed_nodes"],
                "affected_nodes": final_result.report["affected_nodes"],
                "self_check": final_result.report["self_check"],
            }
        )

    clean_bundle = engine.bundle.copy()
    clean_truth_store = TruthStore(
        {
            dialog_id: dialog.truth_variants
            for dialog_id, dialog in clean_bundle.dialogs.items()
        },
        engine.truth_store.corrections,
    )
    full_engine = IncrementalPipeline(
        clean_bundle,
        config=engine.config,
        truth_store=clean_truth_store,
        cache_dir=work_dir / "full-cache",
        config_snapshot_dir=work_dir / "full-config-snapshots",
        lineage_dir=work_dir / "full-lineage",
    )
    clean_full = full_engine.run()
    consistency = compare_runs(final_result, clean_full)

    replay_segment = engine.lineage.replay(original_segment_id, engine.cache)
    replay_dialog = engine.lineage.replay(original_dialog_id, engine.cache)
    if not (
        replay_segment["score"]["valence"] == original_segment_score["valence"]
        and replay_segment["score"]["arousal"] == original_segment_score["arousal"]
        and replay_dialog["score"]["valence"] == original_dialog_score["valence"]
        and replay_dialog["score"]["arousal"] == original_dialog_score["arousal"]
    ):
        raise BusinessError(
            "E_LINEAGE_BROKEN",
            "baseline lineage replay did not reproduce the original score",
            {"missing_key_level": "score_reproduction"},
        )

    code_checks = _business_code_checks(bundle, baseline)
    configs = _config_versions(engine)
    return {
        "status": "ok",
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "numpy": _numpy_version(),
            "offline": True,
            "deterministic_seed_generation": True,
        },
        "dataset": {
            "sample_dir": str(samples_dir.resolve()),
            "dialog_count": len(bundle.dialogs),
            "registration_count": len(engine.bundle.registrations),
            "frame_variants": {
                dialog_id: sorted(dialog.truth_variants)
                for dialog_id, dialog in bundle.dialogs.items()
            },
        },
        "baseline": {
            "config_snapshot_version": baseline.config_snapshot["version"],
            "segments": baseline.segment_scores,
            "dialog_aggregates": baseline.aggregates,
            "invalidation_report": baseline.report,
            "original_segment_record_id": original_segment_id,
            "original_dialog_record_id": original_dialog_id,
        },
        "incremental_events": incremental_events,
        "final": {
            "config": final_result.config.to_dict(),
            "config_snapshot_version": final_result.config_snapshot["version"],
            "segments": final_result.segment_scores,
            "dialog_aggregates": final_result.aggregates,
            "invalidation_report": final_result.report,
        },
        "incremental_vs_full": consistency,
        "lineage": {
            "query": {
                "segment_record_id": original_segment_id,
                "dialog_record_id": original_dialog_id,
            },
            "segment_replay": replay_segment,
            "dialog_replay": replay_dialog,
            "chain_complete": True,
        },
        "cache_versions": configs,
        "business_codes": code_checks,
        "checks": {
            "incremental_equals_full": consistency["matched"],
            "lineage_reproduced": True,
            "invalidation_self_check": final_result.report["self_check"][
                "passed"
            ],
            "segment_aggregate_recompute_matches": all(
                aggregate["independent_recompute"]["matches"]
                for aggregate in final_result.aggregates.values()
            ),
        },
    }


def _business_code_checks(
    bundle: Any, baseline: Any
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def expect(name: str, call: Callable[[], Any]) -> None:
        try:
            call()
        except BusinessError as exc:
            checks.append({"scenario": name, "code": exc.code, "details": exc.details})
        else:
            checks.append(
                {
                    "scenario": name,
                    "code": "NO_ERROR",
                    "details": {"unexpected": True},
                }
            )

    expect("sample_rate_not_16000", lambda: PipelineConfig(sample_rate=8000).validate())
    expect("frame_ms_invalid", lambda: PipelineConfig(frame_ms=0).validate())
    expect(
        "frame_shift_exceeds_frame",
        lambda: PipelineConfig(frame_ms=20.0, frame_shift_ms=25.0).validate(),
    )
    expect(
        "duplicate_dialog_id",
        lambda: validate_dialog_rows(
            [
                {
                    "dialog_id": "dup",
                    "sample_rate": 16000,
                    "duration_ms": 100,
                    "speaker_ids": ["spk_a"],
                    "seed": 1,
                },
                {
                    "dialog_id": "dup",
                    "sample_rate": 16000,
                    "duration_ms": 100,
                    "speaker_ids": ["spk_a"],
                    "seed": 2,
                },
            ]
        ),
    )
    expect(
        "registration_missing_speaker_id",
        lambda: validate_registration_rows(
            [{"register_seed": 1, "register_ms": 200}]
        ),
    )
    expect(
        "interval_order_invalid",
        lambda: validate_interval(0.9, 0.1, dialog_id="dlg-001", segment_index=0),
    )
    first_dialog = bundle.dialogs[sorted(bundle.dialogs)[0]]
    variant_rows = first_dialog.truth_variants[bundle.base_config.frame_variant_key()]
    expect(
        "frame_count_mismatch",
        lambda: feature_product(
            first_dialog.dialog_id,
            first_dialog.pcm,
            bundle.base_config,
            len(variant_rows) + 1,
        ),
    )
    expect(
        "speaker_unknown",
        lambda: validate_assignment_labels(
            first_dialog.dialog_id,
            ["not-a-dialog-speaker"],
            first_dialog.speaker_ids,
        ),
    )
    expect(
        "segment_gap",
        lambda: validate_segments(
            first_dialog.dialog_id,
            [
                {
                    "segment_index": 0,
                    "start_frame": 0,
                    "end_frame": 3,
                },
                {
                    "segment_index": 1,
                    "start_frame": 4,
                    "end_frame": 6,
                },
            ],
            len(variant_rows),
        ),
    )
    expect(
        "resident_memory_resource_limit",
        lambda: feature_product(
            first_dialog.dialog_id,
            first_dialog.pcm,
            PipelineConfig(
                limits=ResourceLimits(max_resident_feature_bytes=1)
            ),
            len(variant_rows),
        ),
    )

    with tempfile.TemporaryDirectory(prefix="dialog-emotion-codes-") as temp:
        root = Path(temp)
        cache = ContentAddressedCache(root / "cache")
        key, components = make_cache_key("demo", {"probe": 1})
        cache.put(key, {"value": 1}, node="demo", key_components=components)
        path = cache.path_for(key)
        corrupted = json.loads(path.read_bytes())
        corrupted["data"]["value"] = 2
        path.write_text(json.dumps(corrupted), encoding="utf-8")
        expect("cache_checksum_corrupt", lambda: cache.get(key))

        busy_cache = ContentAddressedCache(root / "busy-cache")
        busy_key, _ = make_cache_key("demo", {"probe": "busy"})

        def trigger_busy() -> None:
            with busy_cache.stage_lock(busy_key):
                with busy_cache.stage_lock(busy_key):
                    pass

        expect("concurrent_stage_commit", trigger_busy)
        lineage = LineageStore(root / "lineage")
        expect(
            "lineage_record_missing",
            lambda: lineage.get("0" * 64),
        )

    left = copy.deepcopy(baseline)
    right = copy.deepcopy(baseline)
    right.stage_hashes["speaker_assignment:dlg-001"] = "0" * 64
    expect("incremental_drift", lambda: compare_runs(left, right))
    return checks


def _config_versions(engine: IncrementalPipeline) -> dict[str, Any]:
    snapshot = engine.config_store.load_current()
    assert snapshot is not None
    entries = engine.cache.list_entries()
    versions = [entry["version"] for entry in entries]
    return {
        "current_config_snapshot": {
            "version": snapshot["version"],
            "content_hash": snapshot["content_hash"],
            "replacement": snapshot["replacement"],
        },
        "cache_entry_count": len(entries),
        "cache_versions": {
            "min": min(versions) if versions else None,
            "max": max(versions) if versions else None,
            "all_positive": all(version >= 1 for version in versions),
        },
    }


def _first_record(result: Any, *, kind: str, dialog_id: str) -> dict[str, Any]:
    for record in result.lineage_records:
        if record["kind"] == kind and record["dialog_id"] == dialog_id:
            return record
    raise BusinessError(
        "E_LINEAGE_BROKEN",
        "expected lineage record was not produced",
        {"missing_key_level": kind, "dialog_id": dialog_id},
    )


def _change_to_dict(change: Any) -> dict[str, Any]:
    return {"type": type(change).__name__, **change.__dict__}


def _numpy_version() -> str:
    import numpy

    return numpy.__version__


def fail_self_check(report: dict[str, Any]) -> None:
    raise BusinessError(
        "E_INCREMENTAL_DRIFT",
        "invalidation self-check failed",
        {
            "first_divergent_node": "invalidation_self_check",
            "self_check": report["self_check"],
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
