from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cache import CacheStore, compute_cache_key
from .config import ConfigSnapshotStore, PipelineConfig
from .dataset import (
    Dataset,
    DialogSpec,
    corrected_truth,
    load_dataset,
    load_truth,
    load_wav_pcm,
)
from .errors import (
    DomainError,
    E_CACHE_CORRUPT,
    E_INCREMENTAL_DIVERGENCE,
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
)
from .lineage import LineageStore, make_dialog_record, make_segment_record
from .nodes import (
    build_dialog_aggregate,
    build_frame_features,
    build_segment_emotions,
    build_speaker_assignment,
    cross_node_self_check,
)
from .util import (
    canonical_json_bytes,
    read_json,
    sha256_file_chunks,
    sha256_value,
    atomic_write_json,
)


@dataclass
class NodeEvent:
    dialog_id: str
    node: str
    key: str
    key_parts: list[dict[str, Any]]
    payload: dict[str, Any]
    artifact_hash: str
    version: int | None
    reused: bool

    @property
    def event_id(self) -> str:
        return f"{self.dialog_id}:{self.node}"


class NodeExecutor:
    def __init__(
        self,
        config: PipelineConfig,
        cache: CacheStore | None,
        use_cache: bool,
    ) -> None:
        self.config = config
        self.cache = cache
        self.use_cache = use_cache
        self.events: list[NodeEvent] = []
        self.degraded_categories: set[str] = set()
        self.cache_write_skips = 0

    def _put(self, key: str, node: str, payload: dict[str, Any], key_parts: list[dict[str, Any]]) -> dict[str, Any] | None:
        assert self.cache is not None
        try:
            if self.cache.entry_count() >= self.config.max_cache_entries:
                if self.config.resource_policy == "degrade":
                    self.degraded_categories.add("cache_entries_capped")
                    self.cache_write_skips += 1
                    return None
            return self.cache.put(key, node, payload, key_parts)
        except DomainError as exc:
            if exc.code == E_RESOURCE_LIMIT and self.config.resource_policy == "degrade":
                self.degraded_categories.add("cache_entries_capped")
                self.cache_write_skips += 1
                return None
            raise

    def run(
        self,
        dialog_id: str,
        node: str,
        key_parts: list[dict[str, Any]],
        compute: Callable[[], dict[str, Any]],
    ) -> NodeEvent:
        key = compute_cache_key(key_parts)
        if self.use_cache and self.cache is not None:
            existing = self.cache.get(key)
            if existing is not None:
                if existing.get("node") != node:
                    from .errors import E_CACHE_KEY_MISMATCH

                    raise DomainError(
                        E_CACHE_KEY_MISMATCH,
                        "cache node does not match the requested node",
                        entry=key,
                        expected_node=node,
                        stored_node=existing.get("node"),
                    )
                event = NodeEvent(
                    dialog_id=dialog_id,
                    node=node,
                    key=key,
                    key_parts=key_parts,
                    payload=existing["payload"],
                    artifact_hash=existing["artifact_hash"],
                    version=existing.get("version"),
                    reused=True,
                )
                self.events.append(event)
                return event
            payload = compute()
            entry = self._put(key, node, payload, key_parts)
            event = NodeEvent(
                dialog_id=dialog_id,
                node=node,
                key=key,
                key_parts=key_parts,
                payload=payload,
                artifact_hash=sha256_value(payload),
                version=entry.get("version") if entry else None,
                reused=False,
            )
            self.events.append(event)
            return event
        payload = compute()
        event = NodeEvent(
            dialog_id=dialog_id,
            node=node,
            key=key,
            key_parts=key_parts,
            payload=payload,
            artifact_hash=sha256_value(payload),
            version=None,
            reused=False,
        )
        self.events.append(event)
        return event


def _frame_key_parts(config: PipelineConfig, audio_hash: str) -> list[dict[str, Any]]:
    return [
        {"name": "node", "value": "frame_features"},
        {"name": "schema", "value": config.frame_schema_version},
        {"name": "audio_content_hash", "value": audio_hash},
        {"name": "sample_rate", "value": config.sample_rate},
        {"name": "frame_ms", "value": config.frame_ms},
        {"name": "frame_shift_ms", "value": config.frame_shift_ms},
        {"name": "frame_length_samples", "value": config.frame_samples},
        {"name": "frame_shift_samples", "value": config.shift_samples},
        {"name": "fft_size", "value": config.fft_size},
        {"name": "window", "value": config.window},
        {"name": "feature_dtype", "value": config.feature_dtype},
        {"name": "max_feature_memory_mb", "value": config.max_feature_memory_mb},
        {"name": "resource_policy", "value": config.resource_policy},
    ]


def _speaker_key_parts(
    config: PipelineConfig,
    feature_hash: str,
    registry_version: str,
    allowed_speakers: tuple[str, ...],
) -> list[dict[str, Any]]:
    return [
        {"name": "node", "value": "speaker_embedding"},
        {"name": "schema", "value": config.speaker_schema_version},
        {"name": "upstream_frame_features_hash", "value": feature_hash},
        {"name": "registration_version", "value": registry_version},
        {"name": "segment_threshold_ms", "value": config.segment_threshold_ms},
        {"name": "embedding_version", "value": config.embedding_version},
        {"name": "allowed_speakers", "value": list(allowed_speakers)},
    ]


def _segment_key_parts(
    config: PipelineConfig,
    feature_hash: str,
    speaker_hash: str,
    truth_version: str,
    correction_version: str,
) -> list[dict[str, Any]]:
    return [
        {"name": "node", "value": "segment_emotion"},
        {"name": "schema", "value": config.emotion_schema_version},
        {"name": "upstream_frame_features_hash", "value": feature_hash},
        {"name": "upstream_speaker_embedding_hash", "value": speaker_hash},
        {"name": "truth_content_hash", "value": truth_version},
        {"name": "truth_correction_version", "value": correction_version},
        {"name": "regression_ridge", "value": config.regression_ridge},
        {"name": "confidence_level", "value": config.confidence_level},
    ]


def _aggregate_key_parts(config: PipelineConfig, segment_hash: str) -> list[dict[str, Any]]:
    return [
        {"name": "node", "value": "dialog_aggregate"},
        {"name": "schema", "value": config.aggregate_schema_version},
        {"name": "upstream_segment_emotion_hash", "value": segment_hash},
        {"name": "weight_basis", "value": "segment_duration_ms"},
        {"name": "duration_basis", "value": "frame_count * frame_shift_ms"},
    ]


@dataclass
class ExecutionResult:
    dataset: Dataset
    config: PipelineConfig
    dialogs: dict[str, dict[str, Any]]
    events: list[NodeEvent]
    degraded_categories: list[str]
    cache_write_skips: int
    run_id: str
    use_cache: bool

    @property
    def node_events(self) -> list[NodeEvent]:
        return self.events


def _previous_entry(
    cache: CacheStore | None,
    previous_state: dict[str, Any] | None,
    dialog_id: str,
    node: str,
) -> dict[str, Any] | None:
    if cache is None or not previous_state:
        return None
    key = previous_state.get("dialogs", {}).get(dialog_id, {}).get("keys", {}).get(node)
    if not key:
        return None
    try:
        return cache.get(key)
    except DomainError:
        return None


def _execute(
    dataset: Dataset,
    config: PipelineConfig,
    cache: CacheStore | None,
    use_cache: bool,
    previous_state: dict[str, Any] | None,
) -> ExecutionResult:
    executor = NodeExecutor(config, cache, use_cache)
    dialogs: dict[str, dict[str, Any]] = {}
    for dialog in dataset.dialogs:
        pcm, sample_rate, _sample_count = load_wav_pcm(dialog.audio_path)
        if sample_rate != 16000:
            from .errors import E_CONFIG_INVALID

            raise DomainError(
                E_CONFIG_INVALID,
                "audio sample rate must be 16000",
                field="sample_rate",
                dialog_id=dialog.dialog_id,
                value=sample_rate,
            )
        audio_hash = sha256_file_chunks(dialog.audio_path)
        truth_path = dataset.truth_path_for(dialog, config)
        truth_rows = load_truth(truth_path)
        corrected_rows = corrected_truth(truth_rows, dataset.corrections, dialog.dialog_id)
        truth_version = sha256_file_chunks(truth_path)
        correction_version = dataset.corrections_versions[dialog.dialog_id]

        frame_parts = _frame_key_parts(config, audio_hash)
        frame_event = executor.run(
            dialog.dialog_id,
            "frame_features",
            frame_parts,
            lambda: build_frame_features(
                dialog.dialog_id,
                pcm,
                sample_rate,
                len(truth_rows),
                config,
                audio_hash,
            )[0],
        )
        if frame_event.payload.get("degraded"):
            executor.degraded_categories.update(frame_event.payload["degraded"])

        if frame_event.payload["frame_count"] != len(truth_rows):
            from .errors import E_FRAME_COUNT_MISMATCH

            raise DomainError(
                E_FRAME_COUNT_MISMATCH,
                "cached feature frame count does not match the truth frame count",
                dialog_id=dialog.dialog_id,
                expected_frame_count=len(truth_rows),
                actual_frame_count=frame_event.payload["frame_count"],
            )

        speaker_parts = _speaker_key_parts(
            config,
            frame_event.artifact_hash,
            dataset.registry_version,
            dialog.speaker_ids,
        )
        speaker_event = executor.run(
            dialog.dialog_id,
            "speaker_embedding",
            speaker_parts,
            lambda: build_speaker_assignment(
                dialog.dialog_id,
                frame_event.payload,
                dataset.registry,
                dialog.speaker_ids,
                config,
                frame_event.artifact_hash,
            ),
        )
        previous_segment_entry = _previous_entry(
            cache,
            previous_state,
            dialog.dialog_id,
            "segment_emotion",
        )
        reusable_segments = (
            {
                detail["segment_id"]: detail
                for detail in previous_segment_entry.get("payload", {}).get("segments", [])
            }
            if previous_segment_entry
            else {}
        )
        segment_parts = _segment_key_parts(
            config,
            frame_event.artifact_hash,
            speaker_event.artifact_hash,
            truth_version,
            correction_version,
        )
        segment_event = executor.run(
            dialog.dialog_id,
            "segment_emotion",
            segment_parts,
            lambda: build_segment_emotions(
                dialog.dialog_id,
                frame_event.payload,
                speaker_event.payload,
                corrected_rows,
                config,
                frame_event.artifact_hash,
                speaker_event.artifact_hash,
                truth_version,
                correction_version,
                reusable_segments,
            ),
        )
        aggregate_parts = _aggregate_key_parts(config, segment_event.artifact_hash)
        aggregate_event = executor.run(
            dialog.dialog_id,
            "dialog_aggregate",
            aggregate_parts,
            lambda: build_dialog_aggregate(
                dialog.dialog_id,
                segment_event.payload,
                config,
                segment_event.artifact_hash,
            ),
        )
        self_check = cross_node_self_check(
            dialog.dialog_id,
            frame_event.payload,
            speaker_event.payload,
            segment_event.payload,
            aggregate_event.payload,
        )
        dialogs[dialog.dialog_id] = {
            "dialog": dialog,
            "truth_rows": truth_rows,
            "corrected_rows": corrected_rows,
            "truth_path": truth_path,
            "truth_version": truth_version,
            "correction_version": correction_version,
            "audio_hash": audio_hash,
            "frame_event": frame_event,
            "speaker_event": speaker_event,
            "segment_event": segment_event,
            "aggregate_event": aggregate_event,
            "self_check": self_check,
            "reusable_segments": reusable_segments,
        }
    run_id = "run_" + sha256_value(
        {
            "config": config.to_dict(),
            "registry_version": dataset.registry_version,
            "aggregates": {
                dialog_id: value["aggregate_event"].artifact_hash
                for dialog_id, value in sorted(dialogs.items())
            },
        }
    ).split(":", 1)[-1][:24]
    return ExecutionResult(
        dataset=dataset,
        config=config,
        dialogs=dialogs,
        events=executor.events,
        degraded_categories=sorted(executor.degraded_categories),
        cache_write_skips=executor.cache_write_skips,
        run_id=run_id,
        use_cache=use_cache,
    )


def _artifact_hashes(dialog_result: dict[str, Any]) -> dict[str, str]:
    return {
        "frame_features": dialog_result["frame_event"].artifact_hash,
        "speaker_embedding": dialog_result["speaker_event"].artifact_hash,
        "segment_emotion": dialog_result["segment_event"].artifact_hash,
        "dialog_aggregate": dialog_result["aggregate_event"].artifact_hash,
    }


def _cache_keys(dialog_result: dict[str, Any]) -> dict[str, str]:
    return {
        "frame_features": dialog_result["frame_event"].key,
        "speaker_embedding": dialog_result["speaker_event"].key,
        "segment_emotion": dialog_result["segment_event"].key,
        "dialog_aggregate": dialog_result["aggregate_event"].key,
    }


def _build_lineage_records(result: ExecutionResult) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    for dialog_id, value in result.dialogs.items():
        dialog = value["dialog"]
        versions = {
            "audio_content_hash": value["audio_hash"],
            "truth_content_hash": value["truth_version"],
            "truth_correction_version": value["correction_version"],
            "registration_version": result.dataset.registry_version,
            "registry_sample_count": len(result.dataset.registry),
            "dialog_seed": dialog.seed,
            "sample_rate": dialog.sample_rate,
            "config": result.config.to_dict(),
        }
        keys = _cache_keys(value)
        hashes = _artifact_hashes(value)
        dialog_records = [
            make_segment_record(
                dialog_id,
                detail,
                keys,
                hashes,
                versions,
                result.run_id,
            )
            for detail in value["segment_event"].payload["segments"]
        ]
        dialog_records.append(
            make_dialog_record(
                dialog_id,
                value["aggregate_event"].payload,
                keys,
                hashes,
                versions,
                result.run_id,
            )
        )
        records[dialog_id] = dialog_records
    return records


def _first_divergent_segment(
    left: dict[str, Any],
    right: dict[str, Any],
    node: str,
) -> str | None:
    if node in {"segment_emotion", "dialog_aggregate"}:
        left_segments = left["segment_event"].payload.get("segments", [])
        right_segments = right["segment_event"].payload.get("segments", [])
        for left_detail, right_detail in zip(left_segments, right_segments):
            if canonical_json_bytes(left_detail) != canonical_json_bytes(right_detail):
                return left_detail.get("segment_id")
        if len(left_segments) != len(right_segments):
            if len(left_segments) > len(right_segments):
                return left_segments[len(right_segments)].get("segment_id")
            return right_segments[len(left_segments)].get("segment_id")
    return None


def verify_incremental_equivalence(incremental: ExecutionResult, full: ExecutionResult) -> dict[str, Any]:
    node_order = ("frame_features", "speaker_embedding", "segment_emotion", "dialog_aggregate")
    event_lookup = {
        (value["dialog"].dialog_id, node): value
        for value in incremental.dialogs.values()
        for node in node_order
    }
    full_lookup = {
        (value["dialog"].dialog_id, node): value
        for value in full.dialogs.values()
        for node in node_order
    }
    event_field = {
        "frame_features": "frame_event",
        "speaker_embedding": "speaker_event",
        "segment_emotion": "segment_event",
        "dialog_aggregate": "aggregate_event",
    }
    for dialog_id in sorted({dialog_id for dialog_id, _node in event_lookup}):
        for node in node_order:
            left = event_lookup[(dialog_id, node)][event_field[node]].payload
            right = full_lookup[(dialog_id, node)][event_field[node]].payload
            if canonical_json_bytes(left) != canonical_json_bytes(right):
                first_segment = _first_divergent_segment(
                    event_lookup[(dialog_id, node)],
                    full_lookup[(dialog_id, node)],
                    node,
                )
                raise DomainError(
                    E_INCREMENTAL_DIVERGENCE,
                    "incremental result diverges from an uncached full recomputation",
                    dialog_id=dialog_id,
                    node=node,
                    first_divergent_segment=first_segment,
                )
    return {
        "enabled": True,
        "status": "OK",
        "compared_dialogs": sorted({dialog_id for dialog_id, _node in event_lookup}),
        "compared_nodes": list(node_order),
        "first_divergent_segment": None,
    }


def _state_path(cache_root: Path, dataset: Dataset) -> Path:
    fingerprint = sha256_value(
        {
            "dataset_root": str(dataset.root),
            "dialogs": [dialog.dialog_id for dialog in dataset.dialogs],
        }
    ).split(":", 1)[-1][:24]
    return cache_root / "state" / f"state_{fingerprint}.json"


def load_run_state(cache_root: Path, dataset: Dataset) -> dict[str, Any] | None:
    path = _state_path(cache_root, dataset)
    if not path.exists():
        return None
    try:
        state = read_json(path)
    except Exception as exc:
        raise DomainError(
            E_CACHE_CORRUPT,
            "run state is not valid JSON",
            entry="run_state",
            version=None,
        ) from exc
    if state.get("schema_version") != "run-state-v1":
        raise DomainError(
            E_CACHE_CORRUPT,
            "run state schema is invalid",
            entry="run_state",
            version=state.get("version"),
        )
    return state


def _build_state(result: ExecutionResult) -> dict[str, Any]:
    dialogs: dict[str, Any] = {}
    for dialog_id, value in result.dialogs.items():
        dialogs[dialog_id] = {
            "frame_count": value["frame_event"].payload["frame_count"],
            "keys": _cache_keys(value),
            "artifact_hashes": _artifact_hashes(value),
            "truth_version": value["truth_version"],
            "correction_version": value["correction_version"],
            "audio_hash": value["audio_hash"],
            "segment_sources": value["segment_event"].payload.get("source_digests", {}),
            "segment_ranges": {
                detail["segment_id"]: [detail["start_frame"], detail["end_frame"]]
                for detail in value["segment_event"].payload.get("segments", [])
            },
        }
    return {
        "schema_version": "run-state-v1",
        "dataset": {
            "root": str(result.dataset.root),
            "registry_version": result.dataset.registry_version,
            "corrections_versions": result.dataset.corrections_versions,
        },
        "config": result.config.to_dict(),
        "run_id": result.run_id,
        "dialogs": dialogs,
    }


def _change_report(
    result: ExecutionResult,
    previous_state: dict[str, Any] | None,
) -> dict[str, Any]:
    event_ids = [event.event_id for event in result.events]
    recomputed_ids = [event.event_id for event in result.events if not event.reused]
    reused_ids = [event.event_id for event in result.events if event.reused]
    families_recomputed = sorted({event.node for event in result.events if not event.reused})
    families_reused = sorted({event.node for event in result.events if event.reused})
    change_types: list[str] = []
    previous_config = previous_state.get("config") if previous_state else None
    current_config = result.config.to_dict()
    if not previous_state:
        change_types.append("initial")
    else:
        if (
            previous_config.get("frame_ms") != current_config.get("frame_ms")
            or previous_config.get("frame_shift_ms") != current_config.get("frame_shift_ms")
        ):
            change_types.append("frame_shift")
        if previous_config.get("segment_threshold_ms") != current_config.get("segment_threshold_ms"):
            change_types.append("segment_threshold")
        if previous_state.get("dataset", {}).get("registry_version") != result.dataset.registry_version:
            change_types.append("registration_append")
        previous_corrections = previous_state.get("dataset", {}).get("corrections_versions", {})
        for dialog_id, version in result.dataset.corrections_versions.items():
            if previous_corrections.get(dialog_id) != version:
                change_types.append("truth_correction")
                break
        for dialog_id, value in result.dialogs.items():
            previous_dialog = previous_state.get("dialogs", {}).get(dialog_id, {})
            if previous_dialog.get("truth_version") != value["truth_version"]:
                change_types.append("truth_revision")
                break
        if previous_config != current_config and not change_types:
            change_types.append("config_change")
    if not change_types:
        change_types.append("cache_state")

    affected_frames: dict[str, list[dict[str, Any]]] = {
        "frame_features": [],
        "speaker_embedding": [],
        "segment_emotion": [],
        "dialog_aggregate": [],
    }
    affected_segments: dict[str, list[dict[str, Any]]] = {
        "frame_features": [],
        "speaker_embedding": [],
        "segment_emotion": [],
        "dialog_aggregate": [],
    }
    detail_reused = 0
    detail_recomputed = 0
    for dialog_id, value in result.dialogs.items():
        frame_event = value["frame_event"]
        speaker_event = value["speaker_event"]
        segment_event = value["segment_event"]
        aggregate_event = value["aggregate_event"]
        segment_details = segment_event.payload.get("segments", [])
        previous_dialog = previous_state.get("dialogs", {}).get(dialog_id, {}) if previous_state else {}
        previous_sources = previous_dialog.get("segment_sources", {})
        if not segment_event.reused:
            for detail in segment_details:
                if previous_sources.get(detail["segment_id"]) == detail["source_digest"]:
                    detail_reused += 1
                else:
                    detail_recomputed += 1
        if not frame_event.reused:
            affected_frames["frame_features"].append(
                {"dialog_id": dialog_id, "ranges": [[0, frame_event.payload["frame_count"]]]}
            )
        if not speaker_event.reused:
            affected_frames["speaker_embedding"].append(
                {"dialog_id": dialog_id, "ranges": [[0, speaker_event.payload["frame_count"]]]}
            )
        if not segment_event.reused:
            changed = []
            for detail in segment_details:
                if previous_sources.get(detail["segment_id"]) != detail["source_digest"]:
                    changed.append(detail)
            affected_frames["segment_emotion"].append(
                {
                    "dialog_id": dialog_id,
                    "ranges": [[detail["start_frame"], detail["end_frame"]] for detail in changed],
                }
            )
            affected_segments["segment_emotion"].append(
                {"dialog_id": dialog_id, "segment_ids": [detail["segment_id"] for detail in changed]}
            )
        if not aggregate_event.reused:
            affected_frames["dialog_aggregate"].append(
                {"dialog_id": dialog_id, "ranges": [[0, value["frame_event"].payload["frame_count"]]]}
            )
            affected_segments["dialog_aggregate"].append(
                {
                    "dialog_id": dialog_id,
                    "segment_ids": [detail["segment_id"] for detail in segment_details],
                }
            )
    recomputed_set = set(recomputed_ids)
    reused_set = set(reused_ids)
    evaluated_set = set(event_ids)
    disjoint = recomputed_set.isdisjoint(reused_set)
    covers = evaluated_set.issubset(recomputed_set | reused_set)
    if not disjoint or not covers:
        raise DomainError(
            E_INCONSISTENT,
            "invalidation self-check failed",
            category="invalidation_sets",
            reuse_recompute_disjoint=disjoint,
            union_covers_affected=covers,
        )
    per_dialog: dict[str, dict[str, list[str]]] = {}
    for dialog_id in result.dialogs:
        per_dialog[dialog_id] = {
            "recomputed": sorted(event.node for event in result.events if event.dialog_id == dialog_id and not event.reused),
            "reused": sorted(event.node for event in result.events if event.dialog_id == dialog_id and event.reused),
        }
    return {
        "change_types": sorted(set(change_types)),
        "recomputed_nodes": sorted(recomputed_ids),
        "reused_nodes": sorted(reused_ids),
        "recomputed_node_types": families_recomputed,
        "reused_node_types": families_reused,
        "recomputed_events": sorted(recomputed_ids),
        "reused_events": sorted(reused_ids),
        "per_dialog": per_dialog,
        "affected_frames": affected_frames,
        "affected_segments": affected_segments,
        "segment_detail_reuse": {
            "reused": detail_reused,
            "recomputed": detail_recomputed,
        },
        "self_check": {
            "passed": True,
            "reuse_recompute_disjoint": disjoint,
            "union_covers_affected": covers,
            "affected_event_count": len(evaluated_set),
            "reused_event_count": len(reused_set),
            "recomputed_event_count": len(recomputed_set),
        },
    }


def _dialog_report(
    dialog_id: str,
    result: ExecutionResult,
    lineage_records: list[dict[str, Any]],
) -> dict[str, Any]:
    value = result.dialogs[dialog_id]
    dialog = value["dialog"]
    frame_payload = value["frame_event"].payload
    speaker_payload = value["speaker_event"].payload
    segment_payload = value["segment_event"].payload
    aggregate_payload = value["aggregate_event"].payload
    segment_ids = [record["score_id"] for record in lineage_records if record["scope"] == "segment"]
    dialog_ids = [record["score_id"] for record in lineage_records if record["scope"] == "dialog"]
    return {
        "dialog_id": dialog_id,
        "duration_ms": dialog.duration_ms,
        "seed": dialog.seed,
        "speaker_ids": list(dialog.speaker_ids),
        "frame_features": {
            "frame_count": frame_payload["frame_count"],
            "truth_frame_count": len(value["truth_rows"]),
            "frame_length_samples": frame_payload["frame_length_samples"],
            "frame_shift_samples": frame_payload["frame_shift_samples"],
            "feature_names": frame_payload["feature_names"],
            "feature_dtype": frame_payload["feature_dtype"],
            "feature_digest": value["frame_event"].artifact_hash,
            "cache_key": value["frame_event"].key,
            "cache_reused": value["frame_event"].reused,
            "degraded": frame_payload.get("degraded", []),
        },
        "speaker_assignment": {
            "frame_label_count": len(speaker_payload["frame_labels"]),
            "frame_label_digest": sha256_value(speaker_payload["frame_labels"]),
            "segment_count": len(speaker_payload["segments"]),
            "segments": speaker_payload["segments"],
            "speaker_embeddings": speaker_payload["speaker_embeddings"],
            "cache_key": value["speaker_event"].key,
            "cache_reused": value["speaker_event"].reused,
        },
        "segments": segment_payload["segments"],
        "dialog_score": {
            "valence": aggregate_payload["valence"],
            "arousal": aggregate_payload["arousal"],
            "ci_lower_valence": aggregate_payload["ci_lower_valence"],
            "ci_upper_valence": aggregate_payload["ci_upper_valence"],
            "ci_lower_arousal": aggregate_payload["ci_lower_arousal"],
            "ci_upper_arousal": aggregate_payload["ci_upper_arousal"],
            "confidence_level": aggregate_payload["confidence_level"],
            "weight_basis": aggregate_payload["weight_basis"],
            "duration_basis": aggregate_payload["duration_basis"],
            "total_duration_ms": aggregate_payload["total_duration_ms"],
            "sum_weights": aggregate_payload["sum_weights"],
            "weights": aggregate_payload["weights"],
            "independent_recomputation": aggregate_payload["independent_recomputation"],
            "recomputation_consistent": aggregate_payload["recomputation_consistent"],
            "cache_key": value["aggregate_event"].key,
            "cache_reused": value["aggregate_event"].reused,
        },
        "lineage": {
            "segment_score_ids": segment_ids,
            "dialog_score_ids": dialog_ids,
        },
        "cache_keys": _cache_keys(value),
        "artifact_hashes": _artifact_hashes(value),
        "input_versions": {
            "audio_content_hash": value["audio_hash"],
            "truth_content_hash": value["truth_version"],
            "truth_correction_version": value["correction_version"],
            "registration_version": result.dataset.registry_version,
            "registry_sample_count": len(result.dataset.registry),
        },
        "self_check": value["self_check"],
    }


class DialogueEmotionPipeline:
    def __init__(
        self,
        cache_root: str | Path,
        lineage_root: str | Path,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.lineage_root = Path(lineage_root)

    def analyze(
        self,
        dataset_root: str | Path,
        config: PipelineConfig,
        registry_path: str | Path | None = None,
        corrections_path: str | Path | None = None,
        use_cache: bool = True,
        verify_full_recompute: bool = False,
        write_lineage: bool = True,
        write_state: bool = True,
    ) -> dict[str, Any]:
        config.validate()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        dataset = load_dataset(dataset_root, config, registry_path, corrections_path)
        config_store = ConfigSnapshotStore(self.cache_root / "config_snapshots")
        config_snapshot = config_store.save(config)
        cache = CacheStore(self.cache_root, max_entries=config.max_cache_entries) if use_cache else None
        previous_state = load_run_state(self.cache_root, dataset) if use_cache and write_state else None
        result = _execute(dataset, config, cache, use_cache, previous_state)
        if verify_full_recompute:
            full_result = _execute(dataset, config, None, False, None)
            verification = verify_incremental_equivalence(result, full_result)
        else:
            verification = {
                "enabled": False,
                "status": "SKIPPED",
                "compared_dialogs": [],
                "compared_nodes": [],
                "first_divergent_segment": None,
            }
        records = _build_lineage_records(result)
        lineage_store = LineageStore(self.lineage_root)
        if write_lineage:
            for dialog_records in records.values():
                for record in dialog_records:
                    lineage_store.save(record)
        change_report = _change_report(result, previous_state)
        effective_parallel, parallel_degraded = config.effective_parallel_nodes()
        degraded = sorted(set(result.degraded_categories) | set(parallel_degraded))
        if write_state and use_cache:
            state = _build_state(result)
            atomic_write_json(_state_path(self.cache_root, dataset), state)
        dialog_reports = [
            _dialog_report(dialog_id, result, records[dialog_id])
            for dialog_id in sorted(result.dialogs)
        ]
        cache_snapshot = cache.snapshot_summary() if cache is not None else None
        report = {
            "schema_version": "analysis-report-v1",
            "business_code": "OK",
            "run_id": result.run_id,
            "config": config.to_dict(),
            "config_snapshot": {
                "version": config_snapshot["version"],
                "checksum": config_snapshot["checksum"],
            },
            "dataset": {
                "root": str(dataset.root),
                "dialog_count": len(dataset.dialogs),
                "registry_path": str(dataset.registry_path),
                "corrections_path": str(dataset.corrections_path),
                "registry_version": dataset.registry_version,
                "registry_sample_count": len(dataset.registry),
            },
            "dialogs": dialog_reports,
            "invalidation": change_report,
            "incremental_verification": verification,
            "cache": {
                "enabled": cache is not None,
                "root": str(self.cache_root),
                "snapshot": cache_snapshot,
                "write_skips_due_to_limit": result.cache_write_skips,
            },
            "resource": {
                "max_feature_memory_mb": config.max_feature_memory_mb,
                "max_cache_entries": config.max_cache_entries,
                "max_parallel_nodes": config.max_parallel_nodes,
                "requested_parallel_nodes": config.requested_parallel_nodes,
                "effective_parallel_nodes": effective_parallel,
                "resource_policy": config.resource_policy,
                "degraded": bool(degraded),
                "degraded_categories": degraded,
            },
            "self_check": {
                "passed": True,
                "cross_node": [dialog["self_check"] for dialog in dialog_reports],
                "reuse_recompute_disjoint": change_report["self_check"]["reuse_recompute_disjoint"],
                "union_covers_affected": change_report["self_check"]["union_covers_affected"],
                "index_domain": {
                    "frame_index": "zero_based_half_open_samples",
                    "segment_index": "zero_based_half_open_frames",
                },
            },
        }
        return report
