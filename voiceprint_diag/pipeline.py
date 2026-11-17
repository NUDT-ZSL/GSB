"""End-to-end deterministic voiceprint pipeline and diagnostic report builder."""

from __future__ import annotations

import dataclasses
import math
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .aggregation import AggregateResult, AggregationError, aggregate_features, aggregation_cache_key
from .audio import AudioData, AudioFormatError, read_wav, relative_name, scan_wav_files
from .cache import CacheStore, action_sort_key
from .canonical import (
    derived_key,
    read_json,
    round_significant,
    sha256_bytes,
    sha256_file,
    sha256_obj,
    write_json_atomic,
)
from .config import Config, load_config
from .embedding import EmbeddingResult, build_embedding, embedding_cache_key, embedding_report
from .features import (
    FeatureSet,
    extract_features,
    feature_cache_key,
    feature_frame_vector,
    feature_summary,
    quantize_feature_frame,
)
from .fixedpoint import fixed_to_float
from .pairs import PairManifestError, PairSpec, SpeakerGroup, load_pair_manifest, normalise_sample_name
from .similarity import SimilarityResult, compute_similarity, similarity_cache_key
from .snapshots import ConfigMismatchError, SnapshotStore


TOTAL_STAGES = 6


class PipelineInputError(ValueError):
    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class RunOutcome:
    exit_code: int
    status: str
    report: dict[str, Any] | None
    report_path: Path | None
    message: str | None = None


def _milliseconds(start: float) -> int:
    return max(0, int(round((time.perf_counter() - start) * 1000.0)))


def _load_anomalies(raw: Any) -> list[dict[str, Any]]:
    items = raw.get("injections") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise PipelineInputError("E_ANOMALY_MANIFEST", "anomaly manifest must contain an injections array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise PipelineInputError("E_ANOMALY_MANIFEST", "each anomaly must be an object")
        injection_id = item.get("id")
        sample = item.get("sample")
        stage = item.get("stage")
        expected = item.get("expected_error", item.get("code"))
        if not isinstance(injection_id, str) or not injection_id:
            raise PipelineInputError("E_ANOMALY_MANIFEST", "anomaly id must be non-empty")
        if injection_id in seen:
            raise PipelineInputError("E_ANOMALY_MANIFEST", f"duplicate anomaly id: {injection_id}")
        if not isinstance(sample, str) or not sample:
            raise PipelineInputError("E_ANOMALY_MANIFEST", "anomaly sample must be non-empty")
        if not isinstance(stage, str) or not stage:
            raise PipelineInputError("E_ANOMALY_MANIFEST", "anomaly stage must be non-empty")
        if not isinstance(expected, str) or not expected:
            raise PipelineInputError("E_ANOMALY_MANIFEST", "anomaly expected_error must be non-empty")
        normalised = dict(item)
        normalised["sample"] = normalise_sample_name(sample)
        normalised["stage"] = stage
        normalised["expected_error"] = expected
        result.append(normalised)
        seen.add(injection_id)
    return result


def _parallel_ordered(
    jobs: list[tuple[Any, Callable[[], Any]]],
    concurrency: int,
) -> list[tuple[Any, Any | None, BaseException | None]]:
    if not jobs:
        return []
    if concurrency <= 1 or len(jobs) == 1:
        output = []
        for identifier, function in jobs:
            try:
                output.append((identifier, function(), None))
            except BaseException as exc:  # preserved and classified by the caller
                output.append((identifier, None, exc))
        return output
    futures: list[tuple[Any, Future[Any]]] = []
    with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as executor:
        for identifier, function in jobs:
            futures.append((identifier, executor.submit(function)))
        output = []
        for identifier, future in futures:
            try:
                output.append((identifier, future.result(), None))
            except BaseException as exc:
                output.append((identifier, None, exc))
    return output


def _sort_failures(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stage_rank = {
        "audio": 0,
        "feature": 1,
        "aggregation": 2,
        "embedding": 3,
        "similarity": 4,
        "precision": 5,
    }
    return sorted(
        failures,
        key=lambda item: (
            str(item.get("sample", "")),
            stage_rank.get(str(item.get("stage", "")), 99),
            str(item.get("code", "")),
            str(item.get("pair_id", "")),
        ),
    )


def _aggregation_failure_key(feature_key: str, config: Config, bits: int) -> str:
    return derived_key(
        "aggregation_failure/v3",
        feature_key,
        config.aggregation_hash(),
        {"fractional_bits": bits},
    )


def _pair_failure_key(pair: PairSpec, config: Config, bits: int) -> str:
    return derived_key(
        "similarity_failure/v3",
        {
            "id": pair.pair_id,
            "left": pair.left,
            "right": pair.right,
            "relation": pair.relation,
        },
        config.similarity_hash(),
        {"fractional_bits": bits},
    )


def _valid_failure(sample: str, stage: str, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, AggregationError):
        return {
            "sample": sample,
            "stage": stage,
            "code": exc.code,
            "message": str(exc),
            "detail": exc.detail,
        }
    return {
        "sample": sample,
        "stage": stage,
        "code": "E_INTERNAL_STAGE",
        "message": str(exc),
        "detail": {"type": type(exc).__name__},
    }


def _write_stage_state(
    snapshots: SnapshotStore,
    cache: CacheStore,
    *,
    config: Config,
    config_sha256: str,
    injection_sha256: str,
    completed_stages: list[str],
    cursors: dict[str, Any],
    timings: dict[str, int],
    complete: bool,
    interrupted: bool,
) -> None:
    snapshots.write_state(
        config=config,
        config_sha256=config_sha256,
        injection_sha256=injection_sha256,
        completed_stages=completed_stages,
        stage_cursors=cursors,
        product_versions=cache.product_versions(),
        complete=complete,
        interrupted=interrupted,
        timings_ms=timings,
    )


def _commit_stage(
    snapshots: SnapshotStore,
    cache: CacheStore,
    *,
    stage: str,
    stage_index: int,
    config: Config,
    config_sha256: str,
    injection_sha256: str,
    completed_stages: list[str],
    cursors: dict[str, Any],
    timings: dict[str, int],
    stage_keys: list[str],
) -> None:
    if stage not in completed_stages:
        completed_stages.append(stage)
    snapshots.write_stage(
        stage=stage,
        stage_index=stage_index,
        total_stages=TOTAL_STAGES,
        complete=True,
        cursor=cursors.get(stage, {}),
        products=cache.stage_products(stage_keys),
        config=config,
        config_sha256=config_sha256,
        injection_sha256=injection_sha256,
        timings_ms=timings,
    )
    _write_stage_state(
        snapshots,
        cache,
        config=config,
        config_sha256=config_sha256,
        injection_sha256=injection_sha256,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        complete=False,
        interrupted=False,
    )


def _checkpoint_stage(
    snapshots: SnapshotStore,
    cache: CacheStore,
    *,
    stage: str,
    stage_index: int,
    config: Config,
    config_sha256: str,
    injection_sha256: str,
    completed_stages: list[str],
    cursors: dict[str, Any],
    timings: dict[str, int],
    stage_keys: list[str],
) -> None:
    snapshots.write_stage(
        stage=stage,
        stage_index=stage_index,
        total_stages=TOTAL_STAGES,
        complete=False,
        cursor=cursors.get(stage, {}),
        products=cache.stage_products(stage_keys),
        config=config,
        config_sha256=config_sha256,
        injection_sha256=injection_sha256,
        timings_ms=timings,
    )
    _write_stage_state(
        snapshots,
        cache,
        config=config,
        config_sha256=config_sha256,
        injection_sha256=injection_sha256,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        complete=False,
        interrupted=False,
    )


def _feature_diff_items(low: FeatureSet, high_bits: int, low_bits: int, config: Config) -> tuple[dict[str, Any], float]:
    if not low.frames:
        return {"items": {}, "max_abs_difference": 0.0, "mean_abs_difference": 0.0}, 0.0
    field_names = ["log_energy", "zcr", "f0_hz"] + [f"cepstral_{index + 1}" for index in range(config.cepstral_order)]
    maxima = {name: 0.0 for name in field_names}
    totals = {name: 0.0 for name in field_names}
    counts = {name: 0 for name in field_names}
    worst = {name: {"frame_index": -1, "low": 0.0, "high": 0.0, "absolute_difference": 0.0} for name in field_names}
    frame_items: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(low.frames):
        low_q = quantize_feature_frame(frame, low_bits)
        high_q = quantize_feature_frame(frame, high_bits)
        pairs: list[tuple[str, int, int]] = [
            ("log_energy", int(low_q["log_energy_q"]), int(high_q["log_energy_q"])),
            ("zcr", int(low_q["zcr_q"]), int(high_q["zcr_q"])),
            ("f0_hz", int(low_q["f0_hz_q"]), int(high_q["f0_hz_q"])),
        ]
        for index, (left, right) in enumerate(zip(low_q["cepstral_q"], high_q["cepstral_q"])):
            pairs.append((f"cepstral_{index + 1}", int(left), int(right)))
        for name, left_q, right_q in pairs:
            left_value = fixed_to_float(left_q, low_bits)
            right_value = fixed_to_float(right_q, high_bits)
            difference = abs(left_value - right_value)
            maxima[name] = max(maxima[name], difference)
            totals[name] += difference
            counts[name] += 1
            if difference > worst[name]["absolute_difference"]:
                worst[name] = {
                    "frame_index": frame_index,
                    "low": round_significant(left_value, config.feature_significant_digits),
                    "high": round_significant(right_value, config.feature_significant_digits),
                    "absolute_difference": round_significant(difference, config.feature_significant_digits),
                }
        frame_items.append(
            {
                "frame_index": frame_index,
                "absolute_differences": {
                    name: round_significant(
                        abs(
                            fixed_to_float(left_q, low_bits)
                            - fixed_to_float(right_q, high_bits)
                        ),
                        config.feature_significant_digits,
                    )
                    for name, left_q, right_q in pairs
                },
            }
        )
    fields = {
        name: {
            "max_abs_difference": round_significant(maxima[name], config.feature_significant_digits),
            "mean_abs_difference": round_significant(
                totals[name] / counts[name], config.feature_significant_digits
            ),
            "worst": worst[name],
        }
        for name in field_names
    }
    overall = max(maxima.values(), default=0.0)
    return {
        "comparison": "low_q_value_minus_high_q_value",
        "item_count": len(field_names),
        "items": fields,
        "frame_items": frame_items,
        "max_abs_difference": round_significant(overall, config.feature_significant_digits),
    }, overall


def _vector_diff_items(
    low_values: list[int | float],
    high_values: list[int | float],
    *,
    low_bits: int,
    high_bits: int,
    digits: int,
    low_is_q: bool,
    high_is_q: bool,
) -> tuple[list[dict[str, Any]], float, float]:
    items: list[dict[str, Any]] = []
    maximum = 0.0
    total = 0.0
    for index, (low, high) in enumerate(zip(low_values, high_values)):
        low_value = fixed_to_float(int(low), low_bits) if low_is_q else float(low)
        high_value = fixed_to_float(int(high), high_bits) if high_is_q else float(high)
        difference = abs(low_value - high_value)
        maximum = max(maximum, difference)
        total += difference
        items.append(
            {
                "index": index,
                "low": round_significant(low_value, digits),
                "high": round_significant(high_value, digits),
                "absolute_difference": round_significant(difference, digits),
            }
        )
    mean = total / len(items) if items else 0.0
    return items, maximum, mean


def _precision_trace(
    *,
    feature_sets: dict[str, FeatureSet],
    valid_aggregates: dict[str, AggregateResult],
    embeddings: dict[str, EmbeddingResult],
    similarity_results: dict[str, SimilarityResult],
    pairs: list[PairSpec],
    config: Config,
    concurrency: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    high_bits = config.high_fractional_bits
    high_aggregates: dict[str, AggregateResult] = {}
    high_embeddings: dict[str, EmbeddingResult] = {}
    for sample in sorted(valid_aggregates):
        high_aggregates[sample] = aggregate_features(feature_sets[sample], high_bits, config)
        high_embeddings[sample] = build_embedding(high_aggregates[sample], high_bits, config)
    high_similarities: dict[str, SimilarityResult] = {}
    high_jobs: list[tuple[PairSpec, Callable[[], SimilarityResult]]] = []
    for pair in pairs:
        if pair.left in high_embeddings and pair.right in high_embeddings:
            high_jobs.append(
                (
                    pair,
                    lambda pair=pair: compute_similarity(
                        pair,
                        high_embeddings[pair.left],
                        high_embeddings[pair.right],
                        "",
                        "",
                        config,
                    ),
                )
            )
    for pair, result, error in _parallel_ordered(high_jobs, concurrency):
        if error is None and result is not None:
            high_similarities[pair.pair_id] = result
    sample_pairs: dict[str, list[PairSpec]] = {sample: [] for sample in valid_aggregates}
    for pair in pairs:
        if pair.left in sample_pairs:
            sample_pairs[pair.left].append(pair)
        if pair.right in sample_pairs and pair.right != pair.left:
            sample_pairs[pair.right].append(pair)
    traces: list[dict[str, Any]] = []
    precision_failures: list[dict[str, Any]] = []
    precision_events: list[dict[str, Any]] = []
    for sample in sorted(valid_aggregates):
        feature_trace, feature_max = _feature_diff_items(
            feature_sets[sample], high_bits, config.fractional_bits, config
        )
        aggregate_items, aggregate_max, aggregate_mean = _vector_diff_items(
            list(valid_aggregates[sample].stat_vector_q),
            list(high_aggregates[sample].stat_vector_q),
            low_bits=config.fractional_bits,
            high_bits=high_bits,
            digits=config.feature_significant_digits,
            low_is_q=True,
            high_is_q=True,
        )
        embedding_items, embedding_max, embedding_mean = _vector_diff_items(
            list(embeddings[sample].vector_q),
            list(high_embeddings[sample].vector_q),
            low_bits=config.fractional_bits,
            high_bits=high_bits,
            digits=config.feature_significant_digits,
            low_is_q=True,
            high_is_q=True,
        )
        pair_differences: list[dict[str, Any]] = []
        similarity_max = 0.0
        for pair in sample_pairs.get(sample, []):
            low_result = similarity_results.get(pair.pair_id)
            high_result = high_similarities.get(pair.pair_id)
            if low_result is None or high_result is None:
                continue
            difference = abs(low_result.similarity - high_result.similarity)
            similarity_max = max(similarity_max, difference)
            pair_differences.append(
                {
                    "pair_id": pair.pair_id,
                    "peer": pair.right if pair.left == sample else pair.left,
                    "low_similarity": low_result.similarity,
                    "high_similarity": high_result.similarity,
                    "absolute_difference": round_significant(difference, config.feature_significant_digits),
                    "low_decision": low_result.decision,
                    "high_decision": high_result.decision,
                    "reference_decision": low_result.reference_decision,
                }
            )
            if low_result.decision != high_result.decision:
                event = {
                    "code": "E_PRECISION_SENSITIVE",
                    "reason": "LOW_VS_HIGH_PRECISION_DECISION",
                    "sample": pair.left,
                    "peer": pair.right,
                    "pair_id": pair.pair_id,
                    "low_decision": low_result.decision,
                    "high_decision": high_result.decision,
                }
                if not any(
                    item.get("reason") == event["reason"]
                    and item.get("pair_id") == event["pair_id"]
                    for item in precision_events
                ):
                    precision_events.append(event)
            if low_result.reference_decision != low_result.decision:
                event = {
                    "code": "E_PRECISION_SENSITIVE",
                    "reason": "EMBEDDING_LEVEL_VS_FIXED_SIMILARITY_DECISION",
                    "sample": pair.left,
                    "peer": pair.right,
                    "pair_id": pair.pair_id,
                    "reference_decision": low_result.reference_decision,
                    "fixed_decision": low_result.decision,
                    "reference_similarity": low_result.reference_similarity,
                    "fixed_similarity": low_result.similarity,
                }
                if event not in precision_events:
                    precision_events.append(event)
        stage_states = [
            ("feature", feature_max),
            ("aggregation", aggregate_max),
            ("quantization", embedding_max),
            ("similarity", similarity_max),
        ]
        first_exceeded = next(
            (stage for stage, maximum in stage_states if maximum > config.precision_tolerance),
            None,
        )
        overflow_risk_evidence = {
            "aggregation": bool(valid_aggregates[sample].overflow_risk or high_aggregates[sample].overflow_risk),
            "embedding": bool(embeddings[sample].overflow_risk or high_embeddings[sample].overflow_risk),
            "similarity": any(
                similarity_results[pair.pair_id].overflow_risk
                for pair in sample_pairs.get(sample, [])
                if pair.pair_id in similarity_results
            ),
        }
        overflow_risk = any(overflow_risk_evidence.values())
        traces.append(
            {
                "sample": sample,
                "low_fractional_bits": config.fractional_bits,
                "high_fractional_bits": high_bits,
                "precision_tolerance": config.precision_tolerance,
                "stages": {
                    "feature": feature_trace,
                    "aggregation": {
                        "comparison": "stat_vector_q_to_common_real_scale",
                        "item_count": len(aggregate_items),
                        "items": aggregate_items,
                        "max_abs_difference": round_significant(aggregate_max, config.feature_significant_digits),
                        "mean_abs_difference": round_significant(aggregate_mean, config.feature_significant_digits),
                    },
                    "quantization": {
                        "comparison": "unit_embedding_q_to_common_real_scale",
                        "item_count": len(embedding_items),
                        "items": embedding_items,
                        "max_abs_difference": round_significant(embedding_max, config.feature_significant_digits),
                        "mean_abs_difference": round_significant(embedding_mean, config.feature_significant_digits),
                        "low_residual_abs_sum": embeddings[sample].residual_abs_sum,
                        "high_residual_abs_sum": high_embeddings[sample].residual_abs_sum,
                    },
                    "similarity": {
                        "comparison": "fixed_cosine_low_vs_high",
                        "pairs": pair_differences,
                        "max_abs_difference": round_significant(similarity_max, config.feature_significant_digits),
                    },
                },
                "first_exceeded_stage": first_exceeded,
                "error_source": {
                    "primary": "fixed_point_accumulation_overflow_risk" if overflow_risk else "quantization_rounding",
                    "overflow_risk_evidence": overflow_risk_evidence,
                    "quantization_residual_abs_sum": embeddings[sample].residual_abs_sum,
                    "max_projection_dot": embeddings[sample].max_absolute_dot,
                    "max_possible_projection_dot": embeddings[sample].max_possible_absolute_dot,
                    "int64_max": (1 << 63) - 1,
                },
            }
        )
    for event in precision_events:
        precision_failures.append(
            {
                "sample": event["sample"],
                "stage": "precision",
                "code": event["code"],
                "message": event["reason"],
                "pair_id": event.get("pair_id"),
                "peer": event.get("peer"),
                "detail": event,
            }
        )
    return (
        {
            "low_fractional_bits": config.fractional_bits,
            "high_fractional_bits": high_bits,
            "precision_tolerance": config.precision_tolerance,
            "samples": traces,
            "events": precision_events,
        },
        precision_failures,
        precision_events,
    )


def run_pipeline(
    *,
    sample_dir: str | Path,
    config_path: str | Path,
    pairs_path: str | Path,
    anomalies_path: str | Path,
    output_dir: str | Path,
    resume: bool = False,
    stop_event: Any | None = None,
) -> RunOutcome:
    pipeline_start = time.perf_counter()
    config = load_config(config_path)
    config_sha = config.hash()
    pairs_raw = read_json(Path(pairs_path))
    pairs_sha = sha256_obj(pairs_raw)
    anomalies_raw = read_json(Path(anomalies_path))
    anomalies_sha = sha256_obj(anomalies_raw)
    try:
        pairs, speaker_groups = load_pair_manifest(pairs_raw)
    except PairManifestError as exc:
        raise PipelineInputError("E_PAIR_MANIFEST", str(exc)) from exc
    anomalies = _load_anomalies(anomalies_raw)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    snapshots = SnapshotStore(output)
    previous_state = snapshots.load_state()
    snapshot_mode = snapshots.validate_for_resume(
        previous_state,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        explicit_resume=resume,
    )
    cache = CacheStore(output / "cache")
    if snapshot_mode["mode"] == "resume":
        resumed_from_stage = len(previous_state.get("completed_stages", [])) if previous_state else 0
    else:
        resumed_from_stage = 0
    timings: dict[str, int] = {}
    completed_stages: list[str] = []
    cursors: dict[str, Any] = {}
    _write_stage_state(
        snapshots,
        cache,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        complete=False,
        interrupted=False,
    )
    failures: list[dict[str, Any]] = []
    files = scan_wav_files(sample_dir)
    sample_names = [relative_name(path, Path(sample_dir)) for path in files]
    audio_objects: dict[str, AudioData] = {}
    feature_sets: dict[str, FeatureSet] = {}
    aggregates: dict[str, AggregateResult] = {}
    embeddings: dict[str, EmbeddingResult] = {}
    similarity_results: dict[str, SimilarityResult] = {}
    pair_failures: dict[str, dict[str, Any]] = {}
    stage_keys: dict[str, list[str]] = {stage: [] for stage in ("audio", "feature", "aggregation", "embedding", "similarity", "trace", "report")}

    # Stage: audio validation plus frame feature extraction.
    stage_start = time.perf_counter()
    invalid: dict[str, dict[str, Any]] = {}
    feature_jobs: list[tuple[tuple[str, str], Callable[[], dict[str, Any]]]] = []
    feature_keys: dict[str, str] = {}
    feature_labels: dict[str, str] = {}
    for path in files:
        name = relative_name(path, Path(sample_dir))
        try:
            audio = read_wav(path, sample_dir, config)
            audio_objects[name] = audio
            key = feature_cache_key(audio.file_sha256, config)
            feature_keys[name] = key
            feature_labels[name] = f"feature:{name}"
            stage_keys["feature"].append(key)
            if not cache.has(key):
                feature_jobs.append(((name, key), lambda audio=audio: extract_features(audio, config).as_payload()))
        except AudioFormatError as exc:
            file_hash = sha256_file(path)
            key = derived_key(
                "feature_failure/v3",
                file_hash,
                config.feature_hash(),
                {"fractional_bits": config.fractional_bits},
            )
            failure = {
                "sample": name,
                "stage": "audio",
                "code": exc.code,
                "message": str(exc),
                "detail": exc.detail,
            }
            invalid[name] = failure
            failures.append(failure)
            stage_keys["audio"].append(key)
            if not cache.has(key):
                cache.store(kind="feature_failure", key=key, label=f"feature:{name}", payload={"failure": failure})
            else:
                cache.get_or_compute(
                    kind="feature_failure",
                    key=key,
                    label=f"feature:{name}",
                    producer=lambda: {"failure": failure},
                )
    processed = len(invalid)
    for identifier, payload, error in _parallel_ordered(feature_jobs, config.concurrency):
        name, key = identifier
        if error is not None:
            failure = _valid_failure(name, "feature", error)
            failures.append(failure)
            invalid[name] = failure
        else:
            assert payload is not None
            cache.store(kind="features", key=key, label=feature_labels[name], payload=payload)
        processed += 1
        if processed % config.snapshot_interval == 0:
            cursors["feature"] = {"completed": processed, "total": len(files), "ordered_by": "sample_name_ascending"}
            _checkpoint_stage(
                snapshots,
                cache,
                stage="feature",
                stage_index=0,
                config=config,
                config_sha256=config_sha,
                injection_sha256=anomalies_sha,
                completed_stages=completed_stages,
                cursors=cursors,
                timings=timings,
                stage_keys=stage_keys["feature"],
            )
    for name in sorted(audio_objects):
        if name in invalid:
            continue
        payload = cache.get_or_compute(
            kind="features",
            key=feature_keys[name],
            label=feature_labels[name],
            producer=lambda: (_ for _ in ()).throw(RuntimeError("feature cache miss after execution")),
        )
        feature_sets[name] = FeatureSet.from_payload(payload)
    cursors["feature"] = {"completed": len(files), "total": len(files), "ordered_by": "sample_name_ascending"}
    timings["feature"] = _milliseconds(stage_start)
    _commit_stage(
        snapshots,
        cache,
        stage="feature",
        stage_index=0,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        stage_keys=stage_keys["feature"] + stage_keys["audio"],
    )
    if stop_event is not None and stop_event.is_set():
        _write_stage_state(
            snapshots,
            cache,
            config=config,
            config_sha256=config_sha,
            injection_sha256=anomalies_sha,
            completed_stages=completed_stages,
            cursors=cursors,
            timings=timings,
            complete=False,
            interrupted=True,
        )
        return RunOutcome(0, "interrupted", None, None, "handled signal after feature stage")

    # Stage: deterministic speaker-level aggregation.
    stage_start = time.perf_counter()
    aggregate_keys: dict[str, str] = {}
    aggregate_jobs: list[tuple[tuple[str, str, str], Callable[[], dict[str, Any]]]] = []
    for name in sorted(feature_sets):
        feature_key = feature_keys[name]
        aggregate_key = aggregation_cache_key(feature_key, config, config.fractional_bits)
        failure_key = _aggregation_failure_key(feature_key, config, config.fractional_bits)
        aggregate_keys[name] = aggregate_key
        stage_keys["aggregation"].append(aggregate_key)
        if cache.has(aggregate_key) or cache.has(failure_key):
            continue
        aggregate_jobs.append(
            (
                (name, aggregate_key, failure_key),
                lambda name=name: aggregate_features(feature_sets[name], config.fractional_bits, config).as_payload(),
            )
        )
    processed = 0
    for identifier, payload, error in _parallel_ordered(aggregate_jobs, config.concurrency):
        name, aggregate_key, failure_key = identifier
        if error is not None:
            failure = _valid_failure(name, "aggregation", error)
            failures.append(failure)
            cache.store(kind="aggregation_failure", key=failure_key, label=f"aggregation:{name}", payload={"failure": failure})
            stage_keys["aggregation"].append(failure_key)
        else:
            assert payload is not None
            cache.store(kind="aggregation", key=aggregate_key, label=f"aggregation:{name}", payload=payload)
        processed += 1
        if processed % config.snapshot_interval == 0:
            cursors["aggregation"] = {"completed": processed, "total": len(feature_sets), "ordered_by": "sample_name_ascending"}
            _checkpoint_stage(
                snapshots,
                cache,
                stage="aggregation",
                stage_index=1,
                config=config,
                config_sha256=config_sha,
                injection_sha256=anomalies_sha,
                completed_stages=completed_stages,
                cursors=cursors,
                timings=timings,
                stage_keys=stage_keys["aggregation"],
            )
    for name in sorted(feature_sets):
        aggregate_key = aggregate_keys[name]
        failure_key = _aggregation_failure_key(feature_keys[name], config, config.fractional_bits)
        if cache.has(aggregate_key):
            payload = cache.get_or_compute(
                kind="aggregation",
                key=aggregate_key,
                label=f"aggregation:{name}",
                producer=lambda: (_ for _ in ()).throw(RuntimeError("aggregation cache miss")),
            )
            aggregates[name] = AggregateResult.from_payload(payload)
        elif cache.has(failure_key):
            payload = cache.get_or_compute(
                kind="aggregation_failure",
                key=failure_key,
                label=f"aggregation:{name}",
                producer=lambda: (_ for _ in ()).throw(RuntimeError("aggregation failure cache miss")),
            )
            cached_failure = payload["failure"]
            if not any(item.get("sample") == name and item.get("code") == cached_failure.get("code") for item in failures):
                failures.append(cached_failure)
            if failure_key not in stage_keys["aggregation"]:
                stage_keys["aggregation"].append(failure_key)
    cursors["aggregation"] = {"completed": len(feature_sets), "total": len(feature_sets), "ordered_by": "sample_name_ascending"}
    timings["aggregation"] = _milliseconds(stage_start)
    _commit_stage(
        snapshots,
        cache,
        stage="aggregation",
        stage_index=1,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        stage_keys=stage_keys["aggregation"],
    )
    if stop_event is not None and stop_event.is_set():
        _write_stage_state(
            snapshots,
            cache,
            config=config,
            config_sha256=config_sha,
            injection_sha256=anomalies_sha,
            completed_stages=completed_stages,
            cursors=cursors,
            timings=timings,
            complete=False,
            interrupted=True,
        )
        return RunOutcome(0, "interrupted", None, None, "handled signal after aggregation stage")

    # Stage: fixed-point embedding generation.
    stage_start = time.perf_counter()
    embedding_keys: dict[str, str] = {}
    embedding_jobs: list[tuple[tuple[str, str], Callable[[], dict[str, Any]]]] = []
    for name in sorted(aggregates):
        aggregate_key = aggregate_keys[name]
        embedding_key = embedding_cache_key(aggregate_key, config, config.fractional_bits)
        embedding_keys[name] = embedding_key
        stage_keys["embedding"].append(embedding_key)
        if not cache.has(embedding_key):
            embedding_jobs.append(
                (
                    (name, embedding_key),
                    lambda name=name: build_embedding(aggregates[name], config.fractional_bits, config).as_payload(),
                )
            )
    processed = 0
    for identifier, payload, error in _parallel_ordered(embedding_jobs, config.concurrency):
        name, embedding_key = identifier
        if error is not None:
            failures.append(_valid_failure(name, "embedding", error))
        else:
            assert payload is not None
            cache.store(kind="embedding", key=embedding_key, label=f"embedding:{name}", payload=payload)
        processed += 1
        if processed % config.snapshot_interval == 0:
            cursors["embedding"] = {"completed": processed, "total": len(aggregates), "ordered_by": "sample_name_ascending"}
            _checkpoint_stage(
                snapshots,
                cache,
                stage="embedding",
                stage_index=2,
                config=config,
                config_sha256=config_sha,
                injection_sha256=anomalies_sha,
                completed_stages=completed_stages,
                cursors=cursors,
                timings=timings,
                stage_keys=stage_keys["embedding"],
            )
    for name in sorted(aggregates):
        embedding_key = embedding_keys[name]
        if cache.has(embedding_key):
            payload = cache.get_or_compute(
                kind="embedding",
                key=embedding_key,
                label=f"embedding:{name}",
                producer=lambda: (_ for _ in ()).throw(RuntimeError("embedding cache miss")),
            )
            embeddings[name] = EmbeddingResult.from_payload(payload)
    cursors["embedding"] = {"completed": len(aggregates), "total": len(aggregates), "ordered_by": "sample_name_ascending"}
    timings["embedding"] = _milliseconds(stage_start)
    _commit_stage(
        snapshots,
        cache,
        stage="embedding",
        stage_index=2,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        stage_keys=stage_keys["embedding"],
    )
    if stop_event is not None and stop_event.is_set():
        _write_stage_state(
            snapshots,
            cache,
            config=config,
            config_sha256=config_sha,
            injection_sha256=anomalies_sha,
            completed_stages=completed_stages,
            cursors=cursors,
            timings=timings,
            complete=False,
            interrupted=True,
        )
        return RunOutcome(0, "interrupted", None, None, "handled signal after embedding stage")

    # Stage: pair-wise fixed-point cosine similarity.
    stage_start = time.perf_counter()
    similarity_keys: dict[str, str] = {}
    similarity_jobs: list[tuple[tuple[str, str, str], Callable[[], dict[str, Any]]]] = []
    for pair in pairs:
        stage_keys["similarity"].append(pair.pair_id)
        if pair.left not in embeddings or pair.right not in embeddings:
            failure = {
                "sample": pair.left,
                "peer": pair.right,
                "pair_id": pair.pair_id,
                "stage": "similarity",
                "code": "E_SAMPLE_UNAVAILABLE",
                "message": "one or both samples have no usable embedding",
                "detail": {"left": pair.left, "right": pair.right},
            }
            pair_failures[pair.pair_id] = failure
            failures.append(failure)
            failure_key = _pair_failure_key(pair, config, config.fractional_bits)
            stage_keys["similarity"].append(failure_key)
            cache.store(kind="similarity_failure", key=failure_key, label=f"similarity:{pair.pair_id}", payload={"failure": failure})
            continue
        key = similarity_cache_key(
            pair,
            embedding_keys[pair.left],
            embedding_keys[pair.right],
            config,
            config.fractional_bits,
        )
        similarity_keys[pair.pair_id] = key
        stage_keys["similarity"].append(key)
        if not cache.has(key):
            similarity_jobs.append(
                (
                    (pair.pair_id, pair.left, pair.right),
                    lambda pair=pair: compute_similarity(
                        pair,
                        embeddings[pair.left],
                        embeddings[pair.right],
                        embedding_keys[pair.left],
                        embedding_keys[pair.right],
                        config,
                    ).as_payload(),
                )
            )
    processed = 0
    for identifier, payload, error in _parallel_ordered(similarity_jobs, config.concurrency):
        pair_id, left_name, right_name = identifier
        pair = next(item for item in pairs if item.pair_id == pair_id)
        if error is not None:
            failure = _valid_failure(left_name, "similarity", error)
            failure["peer"] = right_name
            failure["pair_id"] = pair_id
            failures.append(failure)
            pair_failures[pair_id] = failure
        else:
            assert payload is not None
            cache.store(kind="similarity", key=similarity_keys[pair_id], label=f"similarity:{pair_id}", payload=payload)
        processed += 1
        if processed % config.snapshot_interval == 0:
            cursors["similarity"] = {"completed": processed, "total": len(similarity_jobs), "ordered_by": "pair_manifest_order"}
            _checkpoint_stage(
                snapshots,
                cache,
                stage="similarity",
                stage_index=3,
                config=config,
                config_sha256=config_sha,
                injection_sha256=anomalies_sha,
                completed_stages=completed_stages,
                cursors=cursors,
                timings=timings,
                stage_keys=stage_keys["similarity"],
            )
    for pair in pairs:
        if pair.pair_id in pair_failures:
            continue
        key = similarity_keys.get(pair.pair_id)
        if key is None:
            continue
        payload = cache.get_or_compute(
            kind="similarity",
            key=key,
            label=f"similarity:{pair.pair_id}",
            producer=lambda: (_ for _ in ()).throw(RuntimeError("similarity cache miss")),
        )
        similarity_results[pair.pair_id] = SimilarityResult.from_payload(payload, pair)
    duplicate_counts: dict[int, int] = {}
    for result in similarity_results.values():
        duplicate_counts[result.similarity_fixed] = duplicate_counts.get(result.similarity_fixed, 0) + 1
    boundary_pairs: list[dict[str, Any]] = []
    for pair in pairs:
        result = similarity_results.get(pair.pair_id)
        if result is None:
            continue
        duplicate = duplicate_counts.get(result.similarity_fixed, 0) > 1
        threshold_tie = bool(result.threshold_tie or result.similarity_fixed == result.threshold_fixed)
        if threshold_tie or duplicate:
            boundary_pairs.append(
                {
                    "pair_id": pair.pair_id,
                    "left": pair.left,
                    "right": pair.right,
                    "relation": pair.relation,
                    "similarity_fixed": result.similarity_fixed,
                    "similarity": result.similarity,
                    "threshold_fixed": result.threshold_fixed,
                    "threshold": config.similarity_threshold,
                    "threshold_tie": threshold_tie,
                    "duplicate_similarity": duplicate,
                    "decision": result.decision,
                    "tie_rule": "SAME_SPEAKER when similarity_fixed >= threshold_fixed",
                }
            )
    cursors["similarity"] = {"completed": len(pairs), "total": len(pairs), "ordered_by": "pair_manifest_order"}
    timings["similarity"] = _milliseconds(stage_start)
    _commit_stage(
        snapshots,
        cache,
        stage="similarity",
        stage_index=3,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        stage_keys=stage_keys["similarity"],
    )
    if stop_event is not None and stop_event.is_set():
        _write_stage_state(
            snapshots,
            cache,
            config=config,
            config_sha256=config_sha,
            injection_sha256=anomalies_sha,
            completed_stages=completed_stages,
            cursors=cursors,
            timings=timings,
            complete=False,
            interrupted=True,
        )
        return RunOutcome(0, "interrupted", None, None, "handled signal after similarity stage")

    # Stage: precision tracing at one configured tier above the working width.
    stage_start = time.perf_counter()
    precision_trace, precision_failures, precision_events = _precision_trace(
        feature_sets=feature_sets,
        valid_aggregates=aggregates,
        embeddings=embeddings,
        similarity_results=similarity_results,
        pairs=pairs,
        config=config,
        concurrency=config.concurrency,
    )
    failures.extend(precision_failures)
    timings["precision_trace"] = _milliseconds(stage_start)
    cursors["trace"] = {"completed": len(precision_trace["samples"]), "total": len(feature_sets), "ordered_by": "sample_name_ascending"}
    _commit_stage(
        snapshots,
        cache,
        stage="trace",
        stage_index=4,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        stage_keys=[],
    )
    if stop_event is not None and stop_event.is_set():
        _write_stage_state(
            snapshots,
            cache,
            config=config,
            config_sha256=config_sha,
            injection_sha256=anomalies_sha,
            completed_stages=completed_stages,
            cursors=cursors,
            timings=timings,
            complete=False,
            interrupted=True,
        )
        return RunOutcome(0, "interrupted", None, None, "handled signal after precision trace")

    # Report assembly.
    failures = _sort_failures(failures)
    sample_entries: list[dict[str, Any]] = []
    for name in sample_names:
        if name in invalid:
            sample_entries.append(
                {
                    "name": name,
                    "status": "failed",
                    "failure_codes": [invalid[name]["code"]],
                }
            )
            continue
        if name not in audio_objects or name not in feature_sets:
            sample_entries.append({"name": name, "status": "failed", "failure_codes": ["E_FEATURE_UNAVAILABLE"]})
            continue
        sample_failures = [item for item in failures if item.get("sample") == name]
        entry: dict[str, Any] = {
            "name": name,
            "status": "ok" if name in embeddings else "partial",
            "audio": audio_objects[name].summary(),
            "feature_summary": feature_summary(feature_sets[name], config.feature_significant_digits),
            "cache_keys": {
                "feature": feature_keys.get(name),
                "aggregation": aggregate_keys.get(name),
                "embedding": embedding_keys.get(name),
            },
            "failure_codes": sorted({item["code"] for item in sample_failures}),
        }
        aggregate = aggregates.get(name)
        if aggregate is not None:
            entry["aggregation"] = {
                "valid_frame_count": aggregate.valid_count,
                "participating_frame_indices": list(aggregate.valid_frame_indices),
                "participating_frame_range": aggregate.frame_range(),
                "statistics": aggregate.statistics,
                "aggregate_fingerprint": aggregate.fingerprint,
                "max_absolute_accumulator": aggregate.max_absolute_accumulator,
                "overflow_risk": aggregate.overflow_risk,
            }
        embedding = embeddings.get(name)
        if embedding is not None:
            entry["embedding"] = embedding_report(embedding, config)
        sample_entries.append(entry)
    pair_entries: list[dict[str, Any]] = []
    false_accepts = 0
    false_rejects = 0
    for pair in pairs:
        result = similarity_results.get(pair.pair_id)
        if result is None:
            failure = pair_failures.get(pair.pair_id, {})
            pair_entries.append(
                {
                    "index": pair.index,
                    "id": pair.pair_id,
                    "left": pair.left,
                    "right": pair.right,
                    "relation": pair.relation,
                    "status": "failed",
                    "failure_codes": [failure.get("code", "E_SAMPLE_UNAVAILABLE")],
                }
            )
            continue
        duplicate = duplicate_counts.get(result.similarity_fixed, 0) > 1
        payload = result.as_payload()
        threshold_tie = bool(result.threshold_tie or result.similarity_fixed == result.threshold_fixed)
        payload["threshold_tie"] = threshold_tie
        payload["boundary"] = bool(threshold_tie or duplicate)
        payload["duplicate_similarity"] = duplicate
        payload["status"] = "ok"
        payload["left_sample_cache_keys"] = {
            "feature": feature_keys.get(pair.left),
            "aggregation": aggregate_keys.get(pair.left),
            "embedding": embedding_keys.get(pair.left),
        }
        payload["right_sample_cache_keys"] = {
            "feature": feature_keys.get(pair.right),
            "aggregation": aggregate_keys.get(pair.right),
            "embedding": embedding_keys.get(pair.right),
        }
        pair_entries.append(payload)
        if pair.relation == "cross" and result.decision == "SAME_SPEAKER":
            false_accepts += 1
        if pair.relation == "same" and result.decision == "DIFFERENT_SPEAKER":
            false_rejects += 1
    anomaly_hits: list[dict[str, Any]] = []
    anomaly_misses: list[dict[str, Any]] = []
    for injection in anomalies:
        matching = [
            failure
            for failure in failures
            if failure.get("code") == injection["expected_error"]
            and failure.get("stage") == injection["stage"]
            and (
                failure.get("sample") == injection["sample"]
                or failure.get("peer") == injection["sample"]
            )
        ]
        if matching:
            anomaly_hits.append(
                {
                    "id": injection["id"],
                    "sample": injection["sample"],
                    "stage": injection["stage"],
                    "expected_error": injection["expected_error"],
                    "failure": matching[0],
                }
            )
        else:
            anomaly_misses.append(dict(injection))
    speaker_group_report: list[dict[str, Any]] = []
    for group in speaker_groups:
        speaker_group_report.append(
            {
                "speaker_id": group.speaker_id,
                "samples": list(group.samples),
                "sample_cache_keys": {
                    sample: {
                        "feature": feature_keys.get(sample),
                        "aggregation": aggregate_keys.get(sample),
                        "embedding": embedding_keys.get(sample),
                    }
                    for sample in group.samples
                },
            }
        )
    same_total = sum(1 for pair in pairs if pair.relation == "same")
    cross_total = sum(1 for pair in pairs if pair.relation == "cross")
    deterministic_results = {
        "schema_version": "1.0",
        "algorithm_version": "fixedpoint-v3",
        "inputs": {
            "sample_count": len(files),
            "sample_names": sample_names,
            "pairs_sha256": pairs_sha,
            "anomalies_sha256": anomalies_sha,
            "injection_sha256": anomalies_sha,
        },
        "samples": sample_entries,
        "pairs": pair_entries,
        "speaker_groups": speaker_group_report,
        "boundary_pairs": boundary_pairs,
        "precision_trace": precision_trace,
        "anomalies": {
            "declared_count": len(anomalies),
            "hit_ids": [item["id"] for item in anomaly_hits],
            "hits": anomaly_hits,
            "misses": anomaly_misses,
        },
        "failures": failures,
        "summary": {
            "same_speaker_pairs": same_total,
            "cross_speaker_pairs": cross_total,
            "false_accepts": false_accepts,
            "false_rejects": false_rejects,
            "boundary_pair_count": len(boundary_pairs),
            "precision_sensitive_count": len(precision_events),
            "failure_count": len(failures),
            "hit_anomaly_count": len(anomaly_hits),
            "declared_anomaly_count": len(anomalies),
        },
    }
    result_fingerprint = sha256_obj(deterministic_results)
    reused = sorted(cache.reuse_records(), key=action_sort_key)
    recomputed = sorted(cache.recompute_records(), key=action_sort_key)
    invalidated = sorted(cache.invalidated(keep_kinds={"features", "feature_failure", "aggregation", "aggregation_failure", "embedding", "similarity", "similarity_failure"}), key=action_sort_key)
    timings["total"] = _milliseconds(pipeline_start)
    report = {
        "schema_version": "1.0",
        "status": "complete",
        "config": config.as_dict(),
        "inputs": {
            "config_sha256": config_sha,
            "pairs_sha256": pairs_sha,
            "anomalies_sha256": anomalies_sha,
        },
        "result_fingerprint": result_fingerprint,
        "deterministic_results": deterministic_results,
        "incremental": {
            "snapshot_mode": snapshot_mode["mode"],
            "resumed_from_stage_count": resumed_from_stage,
            "reused": reused,
            "recomputed": recomputed,
            "invalidated": invalidated,
            "reused_count": len(reused),
            "recomputed_count": len(recomputed),
            "invalidated_count": len(invalidated),
            "rule": "reuse iff the derived cache key exists from a fully replaced artifact",
        },
        "stage_timings_ms": timings,
        "operational_note": "stage_timings_ms, cache action lists, and snapshot resume metadata are execution metadata; deterministic_results is canonical across concurrency and resume points",
    }
    report_path = output / "report.json"
    write_json_atomic(report_path, report)
    write_json_atomic(
        output / "canonical_results.json",
        {"result_fingerprint": result_fingerprint, "deterministic_results": deterministic_results},
    )
    if "report" not in completed_stages:
        completed_stages.append("report")
    cursors["report"] = {"completed": 1, "total": 1, "ordered_by": "none"}
    snapshots.write_stage(
        stage="report",
        stage_index=5,
        total_stages=TOTAL_STAGES,
        complete=True,
        cursor=cursors["report"],
        products={"report": {"label": "report:json", "kind": "report", "payload_sha256": result_fingerprint}},
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        timings_ms=timings,
    )
    _write_stage_state(
        snapshots,
        cache,
        config=config,
        config_sha256=config_sha,
        injection_sha256=anomalies_sha,
        completed_stages=completed_stages,
        cursors=cursors,
        timings=timings,
        complete=True,
        interrupted=False,
    )
    return RunOutcome(0, "complete", report, report_path, None)
