"""Command-line entry points and reproducible smoke verification."""

from __future__ import annotations

import argparse
import copy
import json
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .aggregation import assert_stream_batch_match, first_frame_deviation
from .batch import (
    OfflineEngine,
    find_dialog,
    load_jsonl,
    load_wav,
)
from .cache import ArtifactCache
from .config import config_from_dict, load_config
from .errors import (
    E_CHUNK_GAP,
    E_CHUNK_ORDER,
    E_CHUNK_RANGE,
    E_CONFIG_INVALID,
    E_SEGMENT_BOUNDARY,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_SPEAKER_LABEL_INVALID,
    E_WINDOW_RECOUNT,
    EngineError,
)
from .segments import validate_segment_boundaries
from .snapshot import Snapshot
from .streaming import StreamingEngine
from .synth import generate_samples
from .util import (
    intervals_cover_all,
    intervals_overlap,
    sha256_json,
)
from .windows import recount_window


DEFAULT_CONFIG = "config/default.json"
DEFAULT_MANIFEST = "samples/dialogs.jsonl"
DEFAULT_REPORT = "reports/smoke_report.json"


def _resolve(manifest_path: Path, relative: str) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else manifest_path.parent / path


def _write_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        handle.write("\n")


def _dialog_inputs(manifest_path: Path, dialog_id: str) -> tuple[dict[str, Any], np.ndarray, int, list[dict[str, Any]]]:
    manifest = load_jsonl(manifest_path)
    entry = find_dialog(manifest, dialog_id)
    pcm, sample_rate = load_wav(_resolve(manifest_path, str(entry["wav_path"])))
    truth = load_jsonl(_resolve(manifest_path, str(entry["truth_path"])))
    return entry, pcm, sample_rate, truth


def _report_from_view(
    view: Mapping[str, Any],
    *,
    dialog_id: str,
    execution_path: str,
    latency: Sequence[Mapping[str, Any]],
    deviations: Sequence[Mapping[str, Any]],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": "emotion-engine-report-v1",
        "dialog_id": dialog_id,
        "execution_path": execution_path,
        "business_code": "OK",
        "business_codes": ["OK"],
        "degraded": False,
        "declared_precision": view["config"]["comparison"]["declared_precision"],
        "frame_count": view["frame_count"],
        "summary": view["summary"],
        "frames": view["frames"],
        "segments": view["segments"],
        "window_semantics": view["window_semantics"],
        "diagnostics": view["diagnostics"],
        "latency_observations": list(latency),
        "deviations": list(deviations),
        "cache": view.get("cache"),
        "snapshots": view.get("snapshots"),
        "snapshot_version": view.get("snapshot_version"),
        "snapshot_replacements": view.get("snapshot_replacements"),
        "self_check": view.get("self_check"),
        "verification": view.get("verification"),
        "stream_batch": view.get("stream_batch"),
        "truth_alignment": view.get("truth_alignment"),
        "checksums": view.get("checksums"),
        "audio": view.get("audio"),
    }
    if extra:
        report.update(extra)
    return report


def run_offline(
    config_path: str,
    manifest_path: str,
    dialog_id: str,
    output_path: str | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    manifest = Path(manifest_path)
    entry, pcm, sample_rate, truth = _dialog_inputs(manifest, dialog_id)
    engine = OfflineEngine(config)
    view = engine.analyze_dialog(entry, _resolve(manifest, str(entry["wav_path"])), _resolve(manifest, str(entry["truth_path"])))
    report = _report_from_view(
        view,
        dialog_id=dialog_id,
        execution_path="offline_batch",
        latency=[],
        deviations=[],
    )
    if output_path:
        _write_json(output_path, report)
    return report


def run_stream(
    config_path: str,
    manifest_path: str,
    dialog_id: str,
    output_path: str | None = None,
    offline_check: bool = True,
) -> dict[str, Any]:
    config = load_config(config_path)
    manifest = Path(manifest_path)
    entry, pcm, sample_rate, truth = _dialog_inputs(manifest, dialog_id)
    engine = StreamingEngine(entry, config)
    chunk_samples = round(int(entry["chunk_ms"]) * sample_rate / 1000)
    hop_samples = config.hop_samples(sample_rate)
    sequence = 0
    for start in range(0, int(pcm.shape[0]), chunk_samples):
        end = min(int(pcm.shape[0]), start + chunk_samples)
        labels = [
            str(truth[index]["speaker_id"])
            for index in range(start // hop_samples, min(end // hop_samples, len(truth)))
        ]
        engine.submit_chunk(
            sequence=sequence,
            start_ms=round(start * 1000 / sample_rate),
            pcm=pcm[start:end],
            speaker_ids=labels,
            checkpoint=True,
        )
        sequence += 1
    stream_view = engine.finish()
    offline_view: dict[str, Any] | None = None
    deviation: dict[str, Any] | None = None
    if offline_check:
        offline_engine = OfflineEngine(config)
        offline_view = offline_engine.analyze_dialog(
            entry,
            _resolve(manifest, str(entry["wav_path"])),
            _resolve(manifest, str(entry["truth_path"])),
            verify_recounts=False,
        )
        deviation = first_frame_deviation(stream_view, offline_view, config)
        if deviation is not None:
            raise EngineError(
                "E_STREAM_BATCH_MISMATCH",
                "final streaming view differs from offline full aggregation",
                first_deviation=deviation,
                stream_stats=stream_view["summary"],
                batch_stats=offline_view["summary"],
            )
    report = _report_from_view(
        stream_view,
        dialog_id=dialog_id,
        execution_path="streaming_with_offline_check" if offline_check else "streaming",
        latency=engine.latency_observations,
        deviations=engine.deviations,
        extra={
            "final_offline_match": deviation is None,
            "final_offline_summary": offline_view["summary"] if offline_view else None,
        },
    )
    if output_path:
        _write_json(output_path, report)
    return report


def _check(name: str, function: Callable[[], Any], expected_code: str = "OK") -> dict[str, Any]:
    started = time.perf_counter_ns()
    try:
        value = function()
        actual_code = "OK"
        ok = actual_code == expected_code
        details = value
    except EngineError as exc:
        actual_code = exc.code
        ok = actual_code == expected_code
        details = exc.to_dict()
    except Exception as exc:  # keep smoke diagnostics available
        actual_code = "E_UNEXPECTED"
        ok = False
        details = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=4),
        }
    return {
        "name": name,
        "ok": bool(ok),
        "expected_business_code": expected_code,
        "actual_business_code": actual_code,
        "elapsed_ms": round((time.perf_counter_ns() - started) / 1_000_000.0, 6),
        "details": details,
    }


def run_smoke(
    config_path: str = DEFAULT_CONFIG,
    output_dir: str = "samples",
    report_path: str = DEFAULT_REPORT,
) -> dict[str, Any]:
    smoke_started = time.perf_counter_ns()
    config = load_config(config_path)
    manifest_path = Path(output_dir) / "dialogs.jsonl"
    if not manifest_path.exists():
        generate_samples(output_dir, config)
    manifest = load_jsonl(manifest_path)
    entry = manifest[0]
    dialog_id = str(entry["dialog_id"])
    wav_path = _resolve(manifest_path, str(entry["wav_path"]))
    truth_path = _resolve(manifest_path, str(entry["truth_path"]))
    pcm, sample_rate = load_wav(wav_path)
    truth = load_jsonl(truth_path)
    speakers = [str(item["speaker_id"]) for item in truth]
    allowed_speakers = [str(item) for item in entry["speakers"]]

    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "samples_present_and_16khz_mono_pcm",
            lambda: {
                "manifest_entries": len(manifest),
                "dialog_id": dialog_id,
                "sample_rate": sample_rate,
                "sample_count": int(pcm.shape[0]),
                "truth_frames": len(truth),
                "chunk_ms": int(entry["chunk_ms"]),
            },
        )
    )

    cache = ArtifactCache()
    offline_engine = OfflineEngine(config, cache)

    def offline_once() -> dict[str, Any]:
        return offline_engine.analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed_speakers,
            identity={"dialog_id": dialog_id, "execution_path": "offline_batch"},
            truth=truth,
            verify_recounts=True,
        )

    first_view = offline_once()
    first_checksum = sha256_json(first_view["frames"])
    first_report_checksum = first_view["checksums"]["frames"]
    checks.append(
        _check(
            "offline_window_and_segment_recount",
            lambda: {
                "frames_recounted": len(first_view["verification"]["window_recount"]),
                "segments_recounted": len(first_view["verification"]["segment_recount"]),
            },
        )
    )

    def repeat_offline() -> dict[str, Any]:
        view = offline_once()
        return {
            "frames_sha256": sha256_json(view["frames"]),
            "frames_checksum": view["checksums"]["frames"],
            "cache_mode": view["cache"]["mode"],
            "recomputed_intervals": view["cache"]["recomputed_frame_intervals"],
            "reused_intervals": view["cache"]["reused_frame_intervals"],
        }

    repeat = _check("offline_deterministic_repeat_cache_reuse", repeat_offline)
    repeat_details = repeat["details"] if repeat["ok"] else {}
    deterministic_ok = (
        repeat_details.get("frames_sha256") == first_checksum
        and repeat_details.get("frames_checksum") == first_report_checksum
        and repeat_details.get("cache_mode") == "full_reuse"
    )
    repeat["ok"] = repeat["ok"] and deterministic_ok
    checks.append(repeat)

    threshold_cache_check: dict[str, Any]

    def changed_threshold() -> dict[str, Any]:
        raw = copy.deepcopy(config.raw)
        raw["threshold"]["strategy"] = "strict"
        changed = config_from_dict(raw)
        engine = OfflineEngine(changed, cache)
        view = engine.analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed_speakers,
            identity={"dialog_id": dialog_id, "execution_path": "offline_threshold_variant"},
            verify_recounts=False,
        )
        cache_report = view["cache"]
        coverage = intervals_cover_all(
            cache_report["recomputed_frame_intervals"],
            cache_report["reused_frame_intervals"],
            view["frame_count"],
        )
        overlap = intervals_overlap(
            cache_report["recomputed_frame_intervals"],
            cache_report["reused_frame_intervals"],
        )
        return {
            "cache_mode": cache_report["mode"],
            "recomputed_intervals": cache_report["recomputed_frame_intervals"],
            "reused_intervals": cache_report["reused_frame_intervals"],
            "recomputed_segment_ids": cache_report["recomputed_segment_ids"],
            "coverage_all_frames": coverage,
            "overlap_free": not overlap,
            "feature_cache_hit": view["feature_cache"]["feature_cache_hit"],
        }

    threshold_cache_check = _check("threshold_change_selective_cache_rebuild", changed_threshold)
    checks.append(threshold_cache_check)

    def invalid_threshold_config() -> Any:
        raw = copy.deepcopy(config.raw)
        raw["threshold"]["strategy"] = "unsupported"
        config_from_dict(raw)

    checks.append(_check("invalid_threshold_strategy", invalid_threshold_config, E_CONFIG_INVALID))

    def invalid_speaker_label() -> Any:
        bad = list(speakers)
        bad[0] = "spk_not_declared"
        OfflineEngine(config, ArtifactCache()).analyze_pcm(
            pcm,
            sample_rate,
            bad,
            allowed_speakers,
            verify_recounts=False,
        )

    checks.append(_check("speaker_label_outside_declared_set", invalid_speaker_label, E_SPEAKER_LABEL_INVALID))

    def malformed_segments() -> Any:
        malformed = copy.deepcopy(first_view["segments"])
        if len(malformed) > 1:
            malformed[0]["end_frame"] = int(malformed[1]["start_frame"])
        else:
            malformed[0]["end_frame"] = int(malformed[0]["end_frame"]) - 1
        validate_segment_boundaries(malformed, speakers)

    checks.append(_check("segment_boundary_mismatch", malformed_segments, E_SEGMENT_BOUNDARY))

    def corrupt_window_and_recount() -> Any:
        frames = copy.deepcopy(first_view["frames"])
        frames[0]["window"]["valence_mean"] += 0.5
        recount_window(0, frames, config)

    checks.append(_check("window_recount_detects_corruption", corrupt_window_and_recount, E_WINDOW_RECOUNT))

    stream_engine = StreamingEngine(entry, config)

    def stream_all_chunks() -> dict[str, Any]:
        chunk_samples = round(int(entry["chunk_ms"]) * sample_rate / 1000)
        hop_samples = config.hop_samples(sample_rate)
        sequence = 0
        for start in range(0, int(pcm.shape[0]), chunk_samples):
            end = min(int(pcm.shape[0]), start + chunk_samples)
            labels = [
                str(truth[index]["speaker_id"])
                for index in range(start // hop_samples, min(end // hop_samples, len(truth)))
            ]
            stream_engine.submit_chunk(
                sequence,
                round(start * 1000 / sample_rate),
                pcm[start:end],
                labels,
                checkpoint=True,
            )
            sequence += 1
        view = stream_engine.finish()
        return {
            "chunks": sequence,
            "frames": view["frame_count"],
            "snapshot_version": view["snapshot_version"],
            "stream_batch_matched": view["stream_batch"]["matched"],
            "self_check_ok": view["self_check"]["ok"],
            "latency_observations": len(stream_engine.latency_observations),
        }

    stream_check = _check("stream_chunk_checkpoints_match_offline_prefix", stream_all_chunks)
    checks.append(stream_check)
    stream_view = stream_engine.finish()

    def recount_all_stream_windows() -> dict[str, Any]:
        for index in range(stream_view["frame_count"]):
            stream_engine.recount_frame(index)
        for index in range(len(stream_view["segments"])):
            stream_engine.recount_segment(index)
        return {
            "windows_recounted": stream_view["frame_count"],
            "segments_recounted": len(stream_view["segments"]),
        }

    checks.append(_check("stream_window_and_segment_recount", recount_all_stream_windows))

    def final_stream_offline_match() -> dict[str, Any]:
        deviation = first_frame_deviation(stream_view, first_view, config)
        if deviation is not None:
            raise EngineError(
                "E_STREAM_BATCH_MISMATCH",
                "final stream/offline mismatch",
                first_deviation=deviation,
            )
        return {
            "frame_count": stream_view["frame_count"],
            "summary_equal": stream_view["summary"] == first_view["summary"],
            "segments_equal": stream_view["segments"] == first_view["segments"],
            "tier_switch_sequence_equal": [
                [frame["frame_index"], frame["tier"]] for frame in stream_view["frames"]
            ]
            == [[frame["frame_index"], frame["tier"]] for frame in first_view["frames"]],
        }

    checks.append(_check("final_stream_offline_exact_view", final_stream_offline_match))

    def mismatch_guard() -> Any:
        tampered = copy.deepcopy(stream_view)
        tampered["frames"][2]["tier"] = "forced_mismatch"
        assert_stream_batch_match(tampered, first_view, config)

    checks.append(_check("stream_batch_mismatch_guard", mismatch_guard, "E_STREAM_BATCH_MISMATCH"))

    def bad_chunk_order() -> Any:
        engine = StreamingEngine(entry, config)
        engine.submit_chunk(0, int(entry["chunk_ms"]), pcm[: round(int(entry["chunk_ms"]) * sample_rate / 1000)], ["spk_a"])

    checks.append(_check("chunk_timestamp_inversion", bad_chunk_order, E_CHUNK_ORDER))

    def bad_chunk_gap() -> Any:
        engine = StreamingEngine(entry, config)
        engine.submit_chunk(
            1,
            0,
            pcm[: round(int(entry["chunk_ms"]) * sample_rate / 1000)],
            ["spk_a"] * (round(int(entry["chunk_ms"]) * sample_rate / 1000) // config.hop_samples(sample_rate)),
        )

    checks.append(_check("chunk_sequence_gap", bad_chunk_gap, E_CHUNK_GAP))

    def bad_chunk_range() -> Any:
        engine = StreamingEngine(entry, config)
        oversized = np.zeros(int(pcm.shape[0]) + config.hop_samples(sample_rate), dtype=np.int16)
        engine.submit_chunk(0, 0, oversized, [])

    checks.append(_check("chunk_crosses_dialog_duration", bad_chunk_range, E_CHUNK_RANGE))

    def snapshot_rollback() -> Any:
        stream_engine.snapshots.replace(Snapshot.create("frames", 0, first_view["frames"]))

    checks.append(_check("snapshot_rollback_guard", snapshot_rollback, E_SNAPSHOT_ROLLBACK))

    def snapshot_corrupt() -> Any:
        stream_engine.snapshots.replace(
            Snapshot(
                kind="frames",
                version=stream_engine._snapshot_version + 1,
                payload=[],
                checksum="deliberately-wrong",
            )
        )

    checks.append(_check("snapshot_corruption_guard", snapshot_corrupt, E_SNAPSHOT_CORRUPT))

    summary = {
        "schema_version": "emotion-engine-smoke-report-v1",
        "business_code": "OK",
        "business_codes": sorted(
            {
                check["actual_business_code"]
                for check in checks
                if check["actual_business_code"] != "OK"
            }
            | {"OK"}
        ),
        "degraded": False,
        "all_checks_passed": all(check["ok"] for check in checks),
        "check_count": len(checks),
        "checks": checks,
        "sample": {
            "manifest": str(manifest_path),
            "dialog_id": dialog_id,
            "sample_rate": sample_rate,
            "duration_ms": int(entry["duration_ms"]),
            "chunk_ms": int(entry["chunk_ms"]),
            "frame_count": len(first_view["frames"]),
            "segment_count": len(first_view["segments"]),
            "speakers": allowed_speakers,
        },
        "summary": stream_view["summary"],
        "offline_summary": first_view["summary"],
        "stream_summary": stream_view["summary"],
        "latency_observations": stream_engine.latency_observations,
        "deviations": stream_engine.deviations,
        "cache": first_view.get("cache"),
        "cache_threshold_variant": threshold_cache_check.get("details"),
        "snapshot_metadata": stream_view.get("snapshots"),
        "self_check": stream_view.get("self_check"),
        "checksums": first_view.get("checksums"),
        "stream_checksums": stream_view.get("checksums"),
        "snapshot_version": stream_view.get("snapshot_version"),
        "frames": stream_view["frames"],
        "segments": stream_view["segments"],
        "elapsed_ms": round((time.perf_counter_ns() - smoke_started) / 1_000_000.0, 6),
    }
    if not summary["all_checks_passed"]:
        summary["business_code"] = "E_SMOKE_FAILED"
        summary["degraded"] = True
    _write_json(report_path, summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m emotion_engine",
        description="Deterministic local emotion-tier engine and reproducible sample harness.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic PCM, truth, and JSONL manifest")
    generate.add_argument("--config", default=DEFAULT_CONFIG)
    generate.add_argument("--out-dir", default="samples")
    generate.add_argument("--seed", type=int, default=20261123)

    offline = subparsers.add_parser("offline", help="run whole-dialog batch analysis")
    offline.add_argument("--config", default=DEFAULT_CONFIG)
    offline.add_argument("--manifest", default=DEFAULT_MANIFEST)
    offline.add_argument("--dialog-id", default="dialog_0001")
    offline.add_argument("--json-out")

    stream = subparsers.add_parser("stream", help="run chunk streaming and compare with offline")
    stream.add_argument("--config", default=DEFAULT_CONFIG)
    stream.add_argument("--manifest", default=DEFAULT_MANIFEST)
    stream.add_argument("--dialog-id", default="dialog_0001")
    stream.add_argument("--json-out")

    smoke = subparsers.add_parser("smoke", help="generate/validate samples and run all acceptance checks")
    smoke.add_argument("--config", default=DEFAULT_CONFIG)
    smoke.add_argument("--out-dir", default="samples")
    smoke.add_argument("--json-out", default=DEFAULT_REPORT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            config = load_config(args.config)
            manifest = generate_samples(args.out_dir, config, seed=args.seed)
            print(json.dumps({"business_code": "OK", "dialog_count": len(manifest)}, ensure_ascii=False))
            return 0
        if args.command == "offline":
            report = run_offline(args.config, args.manifest, args.dialog_id, args.json_out)
            if not args.json_out:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "stream":
            report = run_stream(args.config, args.manifest, args.dialog_id, args.json_out)
            if not args.json_out:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "smoke":
            report = run_smoke(args.config, args.out_dir, args.json_out)
            print(
                json.dumps(
                    {
                        "business_code": report["business_code"],
                        "all_checks_passed": report["all_checks_passed"],
                        "check_count": report["check_count"],
                        "report": args.json_out,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0 if report["all_checks_passed"] else 1
    except EngineError as exc:
        error = exc.to_dict()
        error["degraded"] = True
        print(json.dumps(error, ensure_ascii=False, sort_keys=True))
        return 2
    return 1
