"""Pure stage computations; persistence is handled by AnalysisEngine."""

from __future__ import annotations

import math
from typing import Callable

from .aggregation import aggregate_all
from .canonical import checksum_value, round_float
from .config import AnalysisConfig
from .dataset import Dataset, load_audio, load_truth
from .embeddings import load_or_build_embedding_cache
from .errors import BusinessError, E_CONFIG_INVALID, E_RESOURCE_LIMIT, E_SPEAKER_INVALID
from .features import (
    assign_speaker_sequence,
    contiguous_segments,
    extract_energy_zcr,
    frame_count,
    spectral_descriptors,
)
from .regression import regress_segments
from .snapshot import STAGE_ORDER


ProgressCallback = Callable[[str, str, int, int], None]


def validate_dataset_for_config(dataset: Dataset, config: AnalysisConfig) -> None:
    config.validate()
    for record in dataset.dialogs:
        if record.sample_rate != config.sample_rate:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Dialog sample_rate is not supported",
                {
                    "field": "sample_rate",
                    "dialog_id": record.dialog_id,
                    "expected": config.sample_rate,
                    "actual": record.sample_rate,
                },
            )
        if not math.isclose(record.frame_ms, config.frame_ms, rel_tol=0.0, abs_tol=1e-9):
            raise BusinessError(
                E_CONFIG_INVALID,
                "Config frame_ms does not match the truth frame grid",
                {
                    "field": "frame_ms",
                    "dialog_id": record.dialog_id,
                    "expected": config.frame_ms,
                    "actual": record.frame_ms,
                },
            )
    if config.hop_ms > config.frame_ms:
        raise BusinessError(
            E_CONFIG_INVALID,
            "hop_ms must not exceed frame_ms",
            {"field": "hop_ms", "actual": config.hop_ms, "frame_ms": config.frame_ms},
        )


def total_estimated_frames(dataset: Dataset, config: AnalysisConfig) -> int:
    total = 0
    for record in dataset.dialogs:
        samples = int(record.duration_ms * config.sample_rate / 1000)
        total += frame_count(samples, config.frame_samples(), config.hop_samples())
    return total


def enforce_dialog_limit(dataset: Dataset, config: AnalysisConfig, stage: str) -> None:
    limit = config.max_dialogs_per_stage
    count = len(dataset.dialogs)
    if limit > 0 and count > limit:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "Dialog count exceeds the configured stage limit",
            {
                "stage": stage,
                "degradation_category": "dialog_count",
                "limit": limit,
                "observed": count,
            },
        )


def enforce_memory_limit(
    *,
    stage: str,
    category: str,
    observed_bytes: int,
    limit_bytes: int,
) -> None:
    if limit_bytes > 0 and observed_bytes > limit_bytes:
        raise BusinessError(
            E_RESOURCE_LIMIT,
            "Estimated stage memory exceeds the configured limit",
            {
                "stage": stage,
                "degradation_category": category,
                "limit_bytes": limit_bytes,
                "observed_bytes": observed_bytes,
            },
        )


def run_framing_stage(
    dataset: Dataset,
    config: AnalysisConfig,
    progress: ProgressCallback,
) -> dict:
    validate_dataset_for_config(dataset, config)
    enforce_dialog_limit(dataset, config, "framing")
    estimated_frames = total_estimated_frames(dataset, config)
    enforce_memory_limit(
        stage="framing",
        category="resident_features",
        observed_bytes=estimated_frames * 32,
        limit_bytes=config.max_resident_feature_bytes,
    )
    dialogs_out: list[dict] = []
    total = len(dataset.dialogs)
    for position, record in enumerate(dataset.dialogs, 1):
        samples = load_audio(record, config.sample_rate)
        truth = load_truth(record)
        features = extract_energy_zcr(samples, config)
        if len(features) != len(truth):
            raise BusinessError(
                E_CONFIG_INVALID,
                "Frame count does not align with the dialog truth file",
                {
                    "field": "num_frames",
                    "dialog_id": record.dialog_id,
                    "computed_frames": len(features),
                    "truth_frames": len(truth),
                    "frame_ms": config.frame_ms,
                    "hop_ms": config.hop_ms,
                },
            )
        dialogs_out.append(
            {
                "dialog_id": record.dialog_id,
                "sample_rate": config.sample_rate,
                "frame_ms": config.frame_ms,
                "hop_ms": config.hop_ms,
                "frame_samples": config.frame_samples(),
                "hop_samples": config.hop_samples(),
                "num_frames": len(features),
                "features_checksum": checksum_value(features),
                "features": features,
            }
        )
        progress("dialog_completed", "framing", position, total)
    dialog_ids = [item["dialog_id"] for item in dialogs_out]
    return {
        "stage": "framing",
        "config_checksum": config.checksum(),
        "input_fingerprint": dataset.fingerprint,
        "dialog_ids": dialog_ids,
        "dialog_count": len(dialog_ids),
        "num_frames": sum(item["num_frames"] for item in dialogs_out),
        "dialogs": dialogs_out,
    }


def run_segmentation_stage(
    dataset: Dataset,
    config: AnalysisConfig,
    framing_payload: dict,
    cache_dir,
    progress: ProgressCallback,
) -> dict:
    enforce_dialog_limit(dataset, config, "segmentation")
    estimated_frames = int(framing_payload["num_frames"])
    enforce_memory_limit(
        stage="segmentation",
        category="segment_memory",
        observed_bytes=estimated_frames * 96,
        limit_bytes=config.max_segment_memory_bytes,
    )
    cache = load_or_build_embedding_cache(dataset, config, cache_dir)
    statistics = {
        int(speaker_id): {
            "mean": entry["mean"],
            "std": entry["std"],
            "frame_count": entry["frame_count"],
        }
        for speaker_id, entry in cache["speakers"].items()
    }
    controlled_ids = set(dataset.speaker_ids)
    framing_by_id = {item["dialog_id"]: item for item in framing_payload["dialogs"]}
    dialogs_out: list[dict] = []
    total = len(dataset.dialogs)
    for position, record in enumerate(dataset.dialogs, 1):
        expected_frames = int(framing_by_id[record.dialog_id]["num_frames"])
        samples = load_audio(record, config.sample_rate)
        descriptors = spectral_descriptors(samples, config)
        if descriptors.shape[0] != expected_frames:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Framing snapshot frame count no longer matches the audio",
                {
                    "field": "num_frames",
                    "dialog_id": record.dialog_id,
                    "expected": expected_frames,
                    "actual": int(descriptors.shape[0]),
                },
            )
        truth = load_truth(record)
        if len(truth) != expected_frames:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Truth frame count does not match the framing snapshot",
                {
                    "field": "num_frames",
                    "dialog_id": record.dialog_id,
                    "expected": expected_frames,
                    "actual": len(truth),
                },
            )
        for frame_index, speaker_id in enumerate(truth.speaker_id.tolist()):
            if int(speaker_id) not in controlled_ids:
                raise BusinessError(
                    E_SPEAKER_INVALID,
                    "Truth speaker_id is outside the registered speaker set",
                    {
                        "dialog_id": record.dialog_id,
                        "frame_index": frame_index,
                        "speaker_id": int(speaker_id),
                        "controlled_speaker_ids": sorted(controlled_ids),
                    },
                )
        labels = assign_speaker_sequence(descriptors, statistics, config)
        if not set(labels) <= controlled_ids:
            invalid = next(label for label in labels if label not in controlled_ids)
            raise BusinessError(
                E_SPEAKER_INVALID,
                "Assigned speaker_id is outside the registered speaker set",
                {
                    "dialog_id": record.dialog_id,
                    "frame_index": labels.index(invalid),
                    "speaker_id": invalid,
                    "controlled_speaker_ids": sorted(controlled_ids),
                },
            )
        segments, transitions = contiguous_segments(labels, config.frame_ms)
        dialogs_out.append(
            {
                "dialog_id": record.dialog_id,
                "num_frames": expected_frames,
                "speaker_ids": labels,
                "segments": segments,
                "transitions": transitions,
                "labels_checksum": checksum_value(labels),
            }
        )
        progress("dialog_completed", "segmentation", position, total)
    dialog_ids = [item["dialog_id"] for item in dialogs_out]
    return {
        "stage": "segmentation",
        "config_checksum": config.checksum(),
        "registry_version": dataset.registry_version,
        "registry_checksum": dataset.registry_checksum,
        "embedding_version": cache["registry_version"],
        "embedding_checksum": cache["checksum"],
        "dialog_ids": dialog_ids,
        "dialog_count": len(dialog_ids),
        "dialog_truth_speakers_controlled": True,
        "dialogs": dialogs_out,
    }


def run_regression_stage(
    dataset: Dataset,
    config: AnalysisConfig,
    framing_payload: dict,
    segmentation_payload: dict,
    progress: ProgressCallback,
) -> dict:
    enforce_dialog_limit(dataset, config, "regression")
    estimated_frames = int(framing_payload["num_frames"])
    enforce_memory_limit(
        stage="regression",
        category="resident_features",
        observed_bytes=estimated_frames * 48,
        limit_bytes=config.max_resident_feature_bytes,
    )
    segmentation_by_id = {
        item["dialog_id"]: item for item in segmentation_payload["dialogs"]
    }
    dialogs_out: list[dict] = []
    total = len(dataset.dialogs)
    for position, record in enumerate(dataset.dialogs, 1):
        samples = load_audio(record, config.sample_rate)
        dialog = regress_segments(
            record.dialog_id,
            samples,
            segmentation_by_id[record.dialog_id],
            config,
        )
        dialogs_out.append(dialog)
        progress("dialog_completed", "regression", position, total)
    dialog_ids = [item["dialog_id"] for item in dialogs_out]
    return {
        "stage": "regression",
        "config_checksum": config.checksum(),
        "dialog_ids": dialog_ids,
        "dialog_count": len(dialog_ids),
        "dialogs": dialogs_out,
    }


def run_aggregation_stage(
    dataset: Dataset,
    config: AnalysisConfig,
    regression_payload: dict,
) -> dict:
    enforce_dialog_limit(dataset, config, "aggregation")
    report = aggregate_all(
        regression_payload,
        config,
        registry_version=dataset.registry_version,
        registry_checksum=dataset.registry_checksum,
        config_checksum=config.checksum(),
    )
    dialog_ids = [item["dialog_id"] for item in report["dialogs"]]
    return {
        "stage": "aggregation",
        "config_checksum": config.checksum(),
        "dialog_ids": dialog_ids,
        "dialog_count": len(dialog_ids),
        "report": report,
        "report_hash": report["report_hash"],
    }


def assert_dialog_sets(expected: list[str], payload: dict, stage: str) -> None:
    actual = sorted(str(item) for item in payload.get("dialog_ids", []))
    expected_sorted = sorted(expected)
    if actual != expected_sorted:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Committed stage dialog set is inconsistent",
            {
                "field": "dialog_ids",
                "stage": stage,
                "missing": sorted(set(expected_sorted) - set(actual)),
                "extra": sorted(set(actual) - set(expected_sorted)),
            },
        )

