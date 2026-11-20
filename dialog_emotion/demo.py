from __future__ import annotations

import copy
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .cache import CacheStore, compute_cache_key
from .config import ConfigSnapshotStore, PipelineConfig
from .dataset import load_dataset, load_truth, load_wav_pcm
from .errors import (
    DomainError,
    E_CACHE_BUSY,
    E_CACHE_CORRUPT,
    E_CACHE_KEY_MISMATCH,
    E_CONFIG_INVALID,
    E_DUP_DIALOG,
    E_FRAME_COUNT_MISMATCH,
    E_INCREMENTAL_DIVERGENCE,
    E_INCONSISTENT,
    E_INTERVAL_INVALID,
    E_LINEAGE_BROKEN,
    E_REGISTRY_INVALID,
    E_RESOURCE_LIMIT,
    E_SEGMENT_GAP,
    E_SPEAKER_SET_INVALID,
)
from .lineage import LineageStore
from .nodes import (
    build_frame_features,
    cross_node_self_check,
    validate_intervals,
    validate_speaker_output,
)
from .pipeline import DialogueEmotionPipeline, verify_incremental_equivalence
from .synth import append_registry_file, generate_dataset
from .util import (
    atomic_write_json,
    read_json,
    sha256_file_chunks,
    write_jsonl,
)


def _capture(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = fn()
        return {"business_code": "OK", "result": value}
    except DomainError as exc:
        return exc.to_dict()
    except Exception as exc:
        return {
            "business_code": "E_UNEXPECTED",
            "message": str(exc),
            "exception_type": type(exc).__name__,
        }


def _error_case(
    name: str,
    expected_code: str,
    fn: Callable[[], Any],
) -> dict[str, Any]:
    result = _capture(fn)
    actual = result.get("business_code")
    return {
        "name": name,
        "expected_code": expected_code,
        "actual_code": actual,
        "passed": actual == expected_code,
        "result": result,
    }


def _success_case(name: str, expected: str, fn: Callable[[], Any]) -> dict[str, Any]:
    result = _capture(fn)
    actual = result.get("business_code")
    return {
        "name": name,
        "expected_code": expected,
        "actual_code": actual,
        "passed": actual == expected,
        "result": result,
    }


def _report_summary(name: str, report: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "business_code": report["business_code"],
        "run_id": report["run_id"],
        "change_types": report["invalidation"]["change_types"],
        "recomputed_nodes": report["invalidation"]["recomputed_nodes"],
        "reused_nodes": report["invalidation"]["reused_nodes"],
        "recomputed_node_types": report["invalidation"]["recomputed_node_types"],
        "reused_node_types": report["invalidation"]["reused_node_types"],
        "segment_detail_reuse": report["invalidation"]["segment_detail_reuse"],
        "incremental_verification": report["incremental_verification"]["status"],
        "cache_snapshot": report["cache"]["snapshot"],
        "dialog_scores": {
            dialog["dialog_id"]: {
                "valence": dialog["dialog_score"]["valence"],
                "arousal": dialog["dialog_score"]["arousal"],
                "segment_count": len(dialog["segments"]),
            }
            for dialog in report["dialogs"]
        },
    }


def _cache_corruption_case(root: Path) -> dict[str, Any]:
    cache = CacheStore(root / "corrupt")
    parts = [{"name": "node", "value": "test"}, {"name": "payload", "value": 1}]
    key = compute_cache_key(parts)
    cache.put(key, "test", {"value": 1}, parts)
    path = cache.entry_path(key)
    entry = read_json(path)
    entry["payload"]["value"] = 2
    atomic_write_json(path, entry)
    return _error_case("cache_artifact_corrupt", E_CACHE_CORRUPT, lambda: cache.get(key))


def _cache_key_mismatch_case(root: Path) -> dict[str, Any]:
    cache = CacheStore(root / "key_mismatch")
    parts = [{"name": "node", "value": "test"}, {"name": "payload", "value": 1}]
    key = compute_cache_key(parts)
    cache.put(key, "test", {"value": 1}, parts)
    path = cache.entry_path(key)
    entry = read_json(path)
    entry["key_parts"].append({"name": "tampered", "value": True})
    atomic_write_json(path, entry)
    return _error_case("cache_key_cannot_reproduce_parts", E_CACHE_KEY_MISMATCH, lambda: cache.get(key))


def _cache_busy_case(root: Path) -> dict[str, Any]:
    cache = CacheStore(root / "busy")
    parts = [{"name": "node", "value": "test"}, {"name": "payload", "value": 2}]
    key = compute_cache_key(parts)

    def attempt() -> None:
        with cache.write_guard(key):
            cache.put(key, "test", {"value": 2}, parts)

    return _error_case("concurrent_same_key_writer", E_CACHE_BUSY, attempt)


def _config_checksum_case(root: Path) -> dict[str, Any]:
    store = ConfigSnapshotStore(root / "config")
    store.save(PipelineConfig())
    document = read_json(store.current_path)
    document["payload"]["frame_ms"] = 30.0
    atomic_write_json(store.current_path, document)
    return _error_case("config_snapshot_checksum", E_CACHE_CORRUPT, store.load_current)


def _duplicate_dialog_case(root: Path) -> dict[str, Any]:
    dataset = root / "duplicate"
    dataset.mkdir(parents=True)
    row = {
        "dialog_id": "dup",
        "sample_rate": 16000,
        "duration_ms": 20,
        "seed": 1,
        "speaker_ids": ["spk_a"],
        "audio_path": "audio/dup.wav",
        "truth_path": "truth/dup.jsonl",
    }
    write_jsonl(dataset / "dialog.jsonl", [row, row])
    return _error_case("duplicate_dialog_id", E_DUP_DIALOG, lambda: load_dataset(dataset, PipelineConfig()))


def _missing_registry_field_case(root: Path) -> dict[str, Any]:
    dataset = root / "registry_invalid"
    dataset.mkdir(parents=True)
    row = {
        "dialog_id": "d1",
        "sample_rate": 16000,
        "duration_ms": 20,
        "seed": 1,
        "speaker_ids": ["spk_a"],
        "audio_path": "audio/d1.wav",
        "truth_path": "truth/d1.jsonl",
    }
    write_jsonl(dataset / "dialog.jsonl", [row])
    write_jsonl(
        dataset / "registry.jsonl",
        [{"ref_seed": 1, "ref_duration_ms": 1000, "embedding_version": "embed-v1"}],
    )
    return _error_case("registry_missing_speaker_id", E_REGISTRY_INVALID, lambda: load_dataset(dataset, PipelineConfig()))


def _frame_count_case(dataset_root: Path) -> dict[str, Any]:
    dataset = load_dataset(dataset_root, PipelineConfig())
    dialog = dataset.dialogs[0]
    pcm, sample_rate, _ = load_wav_pcm(dialog.audio_path)
    expected = len(load_truth(dialog.truth_path))
    config = PipelineConfig(frame_shift_ms=10.0)
    return _error_case(
        "frame_count_mismatch",
        E_FRAME_COUNT_MISMATCH,
        lambda: build_frame_features(
            dialog.dialog_id,
            pcm,
            sample_rate,
            expected,
            config,
            sha256_file_chunks(dialog.audio_path),
        ),
    )


def _speaker_and_segment_cases(dataset_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = load_dataset(dataset_root, PipelineConfig())
    allowed = set(dataset.dialogs[0].speaker_ids)
    invalid_speaker = _error_case(
        "speaker_outside_dialog_set",
        E_SPEAKER_SET_INVALID,
        lambda: validate_speaker_output(
            ["spk_unknown"],
            [{"segment_id": "s0", "start_frame": 0, "end_frame": 1, "speaker_id": "spk_unknown"}],
            allowed,
            "dlg_001",
        ),
    )
    gap = _error_case(
        "speaker_segment_gap",
        E_SEGMENT_GAP,
        lambda: validate_speaker_output(
            ["spk_a", "spk_b"],
            [
                {"segment_id": "s0", "start_frame": 0, "end_frame": 1, "speaker_id": "spk_a"},
                {"segment_id": "s1", "start_frame": 2, "end_frame": 2, "speaker_id": "spk_b"},
            ],
            allowed,
            "dlg_001",
        ),
    )
    return invalid_speaker, gap


def _interval_case() -> dict[str, Any]:
    segment = {
        "segment_id": "seg_bad",
        "ci_lower_valence": 0.6,
        "ci_upper_valence": 0.4,
        "ci_lower_arousal": -0.1,
        "ci_upper_arousal": 0.2,
    }
    return _error_case("interval_order", E_INTERVAL_INVALID, lambda: validate_intervals([segment], "dlg"))


def _resource_cases(dataset_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    dataset = load_dataset(dataset_root, PipelineConfig())
    dialog = dataset.dialogs[0]
    pcm, sample_rate, _ = load_wav_pcm(dialog.audio_path)
    expected = len(load_truth(dialog.truth_path))
    audio_hash = sha256_file_chunks(dialog.audio_path)
    limited = PipelineConfig(max_feature_memory_mb=0.000001)
    limit = _error_case(
        "feature_memory_limit",
        E_RESOURCE_LIMIT,
        lambda: build_frame_features(dialog.dialog_id, pcm, sample_rate, expected, limited, audio_hash),
    )
    degraded = PipelineConfig(max_feature_memory_mb=0.000001, resource_policy="degrade")

    def run_degraded() -> dict[str, Any]:
        payload, categories = build_frame_features(
            dialog.dialog_id,
            pcm,
            sample_rate,
            expected,
            degraded,
            audio_hash,
        )
        if payload["feature_dtype"] != "float32" or "feature_precision_float32" not in categories:
            raise AssertionError("degraded feature payload did not record the degradation category")
        return {"feature_dtype": payload["feature_dtype"], "categories": categories}

    degrade = _success_case("feature_memory_degrade", "OK", run_degraded)
    return limit, degrade


def _inconsistent_case() -> dict[str, Any]:
    frame = {"frame_count": 2}
    speaker = {"frame_count": 2, "frame_labels": ["spk_a"], "segments": []}
    segment = {"segments": []}
    aggregate = {"weights": []}
    return _error_case(
        "cross_node_frame_domain",
        E_INCONSISTENT,
        lambda: cross_node_self_check("dlg", frame, speaker, segment, aggregate),
    )


def _divergence_case() -> dict[str, Any]:
    def fake(value: float) -> dict[str, Any]:
        segment = {
            "segment_id": "seg_0000",
            "valence": value,
            "arousal": 0.0,
            "ci_lower_valence": value,
            "ci_upper_valence": value,
            "ci_lower_arousal": 0.0,
            "ci_upper_arousal": 0.0,
        }
        dialog = {"dialog_id": "dlg_x"}
        return {
            "dialog": SimpleNamespace(**dialog),
            "frame_event": SimpleNamespace(payload={"features": []}),
            "speaker_event": SimpleNamespace(payload={"labels": []}),
            "segment_event": SimpleNamespace(payload={"segments": [segment]}),
            "aggregate_event": SimpleNamespace(payload={"valence": value}),
        }

    incremental = SimpleNamespace(dialogs={"dlg_x": fake(0.1)})
    full = SimpleNamespace(dialogs={"dlg_x": fake(0.2)})
    return _error_case(
        "incremental_full_divergence",
        E_INCREMENTAL_DIVERGENCE,
        lambda: verify_incremental_equivalence(incremental, full),
    )


def _lineage_broken_case(lineage_root: Path, score_id: str, root: Path) -> dict[str, Any]:
    empty_cache = CacheStore(root / "empty_cache")
    return _error_case(
        "lineage_chain_break",
        E_LINEAGE_BROKEN,
        lambda: LineageStore(lineage_root).replay(score_id, empty_cache),
    )


def run_demo(workspace: str | Path = ".") -> dict[str, Any]:
    root = Path(workspace).resolve()
    data = root / "data"
    artifacts = root / "artifacts"
    cache_root = artifacts / "demo_cache"
    lineage_root = artifacts / "demo_lineage"
    reports_root = artifacts / "reports"
    temp_root = artifacts / "demo_temp"
    for path in (cache_root, lineage_root, reports_root, temp_root):
        if path.exists():
            shutil.rmtree(path)
    artifacts.mkdir(parents=True, exist_ok=True)
    reports_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    manifest = generate_dataset(data, force=True)
    pipeline = DialogueEmotionPipeline(cache_root, lineage_root)
    reports: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}

    baseline = pipeline.analyze(data, PipelineConfig(), verify_full_recompute=True)
    reports["baseline"] = baseline
    summaries["baseline"] = _report_summary("baseline", baseline)
    atomic_write_json(reports_root / "baseline.json", baseline)

    first_segment = baseline["dialogs"][0]["segments"][0]
    correction_start = first_segment["start_frame"] + 1
    correction_end = min(first_segment["end_frame"], correction_start + 3)
    corrections_path = data / "corrections_update1.jsonl"
    write_jsonl(
        corrections_path,
        [
            {
                "dialog_id": baseline["dialogs"][0]["dialog_id"],
                "start_frame": correction_start,
                "end_frame": correction_end,
                "valence": 0.93,
                "arousal": -0.88,
                "version": "corr-1",
            }
        ],
    )
    corrected = pipeline.analyze(
        data,
        PipelineConfig(),
        corrections_path=corrections_path,
        verify_full_recompute=True,
    )
    reports["truth_correction"] = corrected
    summaries["truth_correction"] = _report_summary("truth_correction", corrected)
    atomic_write_json(reports_root / "truth_correction.json", corrected)

    registry_path = data / "registry_appended.jsonl"
    append_registry_file(
        data,
        registry_path,
        {
            "speaker_id": "spk_a",
            "ref_seed": 7199,
            "ref_duration_ms": 1200,
            "embedding_version": "embed-v1",
        },
    )
    registration = pipeline.analyze(
        data,
        PipelineConfig(),
        registry_path=registry_path,
        corrections_path=corrections_path,
        verify_full_recompute=True,
    )
    reports["registration_append"] = registration
    summaries["registration_append"] = _report_summary("registration_append", registration)
    atomic_write_json(reports_root / "registration_append.json", registration)

    frame_shift = pipeline.analyze(
        data,
        PipelineConfig(frame_shift_ms=10.0),
        registry_path=registry_path,
        corrections_path=corrections_path,
        verify_full_recompute=True,
    )
    reports["frame_shift"] = frame_shift
    summaries["frame_shift"] = _report_summary("frame_shift", frame_shift)
    atomic_write_json(reports_root / "frame_shift.json", frame_shift)

    threshold = pipeline.analyze(
        data,
        PipelineConfig(frame_shift_ms=10.0, segment_threshold_ms=220.0),
        registry_path=registry_path,
        corrections_path=corrections_path,
        verify_full_recompute=True,
    )
    reports["segment_threshold"] = threshold
    summaries["segment_threshold"] = _report_summary("segment_threshold", threshold)
    atomic_write_json(reports_root / "segment_threshold.json", threshold)

    lineage_store = LineageStore(lineage_root)
    cache = CacheStore(cache_root, read_only=True)
    baseline_segment_id = baseline["dialogs"][0]["lineage"]["segment_score_ids"][0]
    baseline_dialog_id = baseline["dialogs"][0]["lineage"]["dialog_score_ids"][0]
    before = cache.snapshot_summary()
    segment_replay = lineage_store.replay(baseline_segment_id, cache)
    dialog_replay = lineage_store.replay(baseline_dialog_id, cache)
    after = cache.snapshot_summary()
    lineage_replay = {
        "summary": {
            "status": "OK",
            "segment_score_id": baseline_segment_id,
            "dialog_score_id": baseline_dialog_id,
            "cache_unchanged": before == after,
        },
        "segment_replay": segment_replay,
        "dialog_replay": dialog_replay,
        "cache_before": before,
        "cache_after": after,
    }

    error_cases: list[dict[str, Any]] = []
    error_cases.append(
        _error_case(
            "sample_rate",
            E_CONFIG_INVALID,
            lambda: PipelineConfig(sample_rate=22050).validate(),
        )
    )
    error_cases.append(
        _error_case(
            "frame_shift_exceeds_frame",
            E_CONFIG_INVALID,
            lambda: PipelineConfig(frame_shift_ms=25.0).validate(),
        )
    )
    error_cases.append(_duplicate_dialog_case(temp_root))
    error_cases.append(_missing_registry_field_case(temp_root))
    error_cases.append(_frame_count_case(data))
    invalid_speaker, segment_gap = _speaker_and_segment_cases(data)
    error_cases.extend([invalid_speaker, segment_gap])
    error_cases.append(_interval_case())
    resource_limit, resource_degrade = _resource_cases(data)
    error_cases.append(resource_limit)
    error_cases.append(_inconsistent_case())
    error_cases.append(_cache_corruption_case(temp_root))
    error_cases.append(_cache_key_mismatch_case(temp_root))
    error_cases.append(_cache_busy_case(temp_root))
    error_cases.append(_config_checksum_case(temp_root))
    error_cases.append(_divergence_case())
    error_cases.append(_lineage_broken_case(lineage_root, baseline_segment_id, temp_root))
    passed = sum(1 for case in error_cases if case["passed"]) + (1 if resource_degrade["passed"] else 0)
    total = len(error_cases) + 1
    all_runs_ok = all(run["business_code"] == "OK" for run in reports.values())
    all_verified = all(
        report["incremental_verification"]["status"] == "OK"
        for report in reports.values()
    )
    lineage_ok = (
        lineage_replay["summary"]["status"] == "OK"
        and lineage_replay["summary"]["cache_unchanged"]
    )
    overall_ok = all_runs_ok and all_verified and lineage_ok and passed == total
    demo_report = {
        "schema_version": "demo-report-v1",
        "business_code": "OK" if overall_ok else "E_DEMO_FAILED",
        "manifest": manifest,
        "runs": {
            name: {"summary": summaries[name], "report_path": str(reports_root / f"{name}.json")}
            for name in reports
        },
        "lineage_replay": lineage_replay,
        "error_cases": {
            "passed": passed,
            "failed": total - passed,
            "total": total,
            "cases": error_cases,
            "degrade_case": resource_degrade,
        },
        "acceptance": {
            "all_runs_ok": all_runs_ok,
            "all_incremental_equivalence_checks_ok": all_verified,
            "lineage_replay_read_only": lineage_ok,
            "error_code_matrix_passed": passed == total,
        },
    }
    atomic_write_json(artifacts / "demo_report.json", demo_report)
    return demo_report
