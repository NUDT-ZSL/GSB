"""Four-stage orchestration with incremental reuse and historical replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .audio import audio_identity, read_pcm
from .cache import CacheEntry, CacheStore, ConfigStore
from .canonical import canonical_json, hash_value, read_json, write_json_atomic
from .config import (
    AggregationConfig,
    PipelineConfig,
    SAMPLE_RATE,
    check_feature_memory,
    resource_limit_error,
)
from .dataset import (
    Dataset,
    assert_enrollment_coverage,
    load_truth,
    truth_frame_hash,
    truth_semantic_hash,
)
from .errors import (
    E_CACHE_CORRUPT,
    E_DUPLICATE_DIALOG,
    E_FRAME_COUNT,
    E_INCONSISTENT,
    E_INPUT_INVALID,
    E_LINEAGE_BROKEN,
    E_RESOURCE_LIMIT,
    E_SPEAKER_UNKNOWN,
    BusinessError,
    raise_error,
)
from .features import FeatureResult, extract_features, frame_count_for
from .keys import (
    assignment_key,
    dialog_key,
    feature_key,
    segment_key,
)
from .lineage import (
    LINEAGE_SCHEMA,
    LineageStore,
    make_lineage_id,
    replay_lineage,
)
from .regression import aggregate_segments, regress_segment
from .speaker import (
    AssignmentResult,
    build_speaker_registry,
    assign_speakers,
)


RUN_SCHEMA = "vem.analysis-run.v1"
RUN_RECORD_SCHEMA = "vem.run-record.v1"


@dataclass(frozen=True)
class RunResult:
    data: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.data["run_id"])

    @property
    def result_fingerprint(self) -> str:
        return str(self.data["result_fingerprint"])

    def to_dict(self) -> dict[str, Any]:
        return self.data

    def canonical_json(self) -> str:
        return canonical_json(self.data)


def _truth_slice_hash(rows: Sequence[dict[str, Any]]) -> str:
    normalized = [
        {
            "frame_index": int(row["frame_index"]),
            "speaker_id": int(row["speaker_id"]),
            "valence": float(row["valence"]),
            "arousal": float(row["arousal"]),
            "is_speech": bool(row["is_speech"]),
        }
        for row in rows
    ]
    return "truthslice-" + hash_value({"kind": "truth-slice", "rows": normalized})[:24]


def _node_id(dialog_id: str, node: str, segment_id: str | None = None) -> str:
    return f"{dialog_id}|{node}|{segment_id or '-'}"


def _path_item(
    *,
    node: str,
    entry: CacheEntry,
    parents: Sequence[str],
    segment_id: str | None = None,
) -> dict[str, Any]:
    output_hash = entry.payload.get("output_hash")
    return {
        "node": node,
        "segment_id": segment_id,
        "cache_key": entry.cache_key,
        "version": int(entry.version),
        "output_hash": output_hash,
        "parents": [str(item) for item in parents],
    }


class VoiceEmotionPipeline:
    """Local, deterministic dialogue emotion analysis component."""

    def __init__(
        self,
        cache_dir: str | Path,
        config: PipelineConfig | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.config.validate(SAMPLE_RATE)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache = CacheStore(self.cache_dir, limits=self.config.resources)
        self.config_store = ConfigStore(self.cache_dir)
        self.lineage = LineageStore(self.cache_dir)
        self.runs_dir = self.cache_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _result_fingerprint(self, dialogs: Sequence[dict[str, Any]]) -> str:
        body = []
        for dialog in sorted(dialogs, key=lambda item: item["dialog_id"]):
            body.append(
                {
                    "dialog_id": dialog["dialog_id"],
                    "truth_version": dialog["truth_version"],
                    "feature_output_hash": dialog["feature"]["output_hash"],
                    "assignment_output_hash": dialog["assignment"]["output_hash"],
                    "segment_output_hashes": [
                        segment["output_hash"] for segment in dialog["segments"]
                    ],
                    "dialog_output_hash": dialog["dialog"]["output_hash"],
                    "labels": dialog["assignment"]["labels"],
                    "segments": [
                        {
                            "segment_id": segment["segment_id"],
                            "start_frame": segment["start_frame"],
                            "end_frame": segment["end_frame"],
                            "speaker_id": segment["speaker_id"],
                            "valence": segment["valence"],
                            "arousal": segment["arousal"],
                        }
                        for segment in dialog["segments"]
                    ],
                    "scores": dialog["dialog"]["scores"],
                }
            )
        return "result-" + hash_value(body)

    def _context_versions(
        self,
        *,
        enrollment_version: str,
        dataset_enrollment_version: str,
        truth_version: str,
        config_version: int,
    ) -> dict[str, Any]:
        return {
            "feature_params_version": self.config.feature.version,
            "feature_params_hash": self.config.feature.params_hash(),
            "enrollment_version": enrollment_version,
            "dataset_enrollment_version": dataset_enrollment_version,
            "truth_correction_version": truth_version,
            "truth_version": truth_version,
            "frame_shift_ms": float(self.config.feature.frame_shift_ms),
            "segment_threshold": int(self.config.speaker.segment_threshold),
            "speaker_config_hash": self.config.speaker.params_hash(),
            "regression_config_hash": self.config.regression.params_hash(),
            "aggregation_config_hash": self.config.aggregation.params_hash(),
            "pipeline_config_hash": self.config.params_hash(),
            "config_version": int(config_version),
        }

    def _cache_put(
        self,
        *,
        key: Any,
        node: str,
        context_versions: dict[str, Any],
        payload: dict[str, Any],
        degraded_categories: set[str],
    ) -> CacheEntry | None:
        allow_degrade = self.config.resources.on_limit == "degrade"
        entry = self.cache.put(
            cache_key=key.cache_key,
            node=node,
            key_components=key.components,
            context_versions=context_versions,
            payload=payload,
            allow_degrade=allow_degrade,
        )
        if entry is None:
            degraded_categories.add("cache_retention_limited")
        return entry

    def _add_status(
        self,
        statuses: list[dict[str, Any]],
        *,
        node: str,
        dialog_id: str,
        cache_key: str,
        reused: bool,
        entry: CacheEntry | None,
        segment_id: str | None = None,
    ) -> None:
        statuses.append(
            {
                "node_id": _node_id(dialog_id, node, segment_id),
                "node": node,
                "dialog_id": dialog_id,
                "segment_id": segment_id,
                "cache_key": cache_key,
                "reused": bool(reused),
                "version": int(entry.version) if entry is not None else None,
            }
        )

    def _load_previous(
        self,
        previous_run: RunResult | Mapping[str, Any] | None,
        previous_run_id: str | None,
    ) -> dict[str, Any] | None:
        if previous_run is not None and previous_run_id is not None:
            raise_error(
                E_INCONSISTENT,
                "provide previous_run or previous_run_id, not both",
            )
        if previous_run_id is not None:
            return self.load_run(previous_run_id).data
        if previous_run is None:
            return None
        if isinstance(previous_run, RunResult):
            return previous_run.data
        data = dict(previous_run)
        if data.get("schema") != RUN_SCHEMA:
            raise_error(
                E_INCONSISTENT,
                "previous run has an unexpected schema",
                schema=data.get("schema"),
            )
        return data

    def analyze_dataset(
        self,
        dataset: Dataset,
        *,
        previous_run: RunResult | Mapping[str, Any] | None = None,
        previous_run_id: str | None = None,
        force_full_recompute: bool = False,
    ) -> RunResult:
        self.config.validate(SAMPLE_RATE)
        dialog_ids = [dialog.dialog_id for dialog in dataset.dialogs]
        if len(dialog_ids) != len(set(dialog_ids)):
            raise_error(
                E_DUPLICATE_DIALOG,
                "dataset contains duplicate dialog_id values",
            )
        assert_enrollment_coverage(dataset.dialogs, dataset.enrollments)
        previous = self._load_previous(previous_run, previous_run_id)
        previous_dialogs = {
            str(item["dialog_id"]): item
            for item in (previous or {}).get("dialogs", [])
        }
        config_snapshot = self.config_store.put(
            {"kind": "voice-emotion-pipeline-config", **self.config.to_dict()}
        )
        config_version = int(config_snapshot["version"])
        config_checksum = str(config_snapshot["checksum"])
        registry = build_speaker_registry(
            dataset.enrollments,
            self.config.feature,
            dataset.enrollment_version,
            sample_rate=SAMPLE_RATE,
        )
        dialogs_out: list[dict[str, Any]] = []
        all_statuses: list[dict[str, Any]] = []
        affected_frames_report: dict[str, list[int]] = {}
        affected_segments_report: dict[str, list[str]] = {}
        changed_dialog_ids: list[str] = []
        changed_node_ids: list[str] = []
        degraded_categories: set[str] = set()

        for dialog in dataset.dialogs:
            try:
                audio = read_pcm(dialog.audio_path)
            except BusinessError:
                raise
            except Exception as exc:
                raise_error(
                    E_INPUT_INVALID,
                    "audio input cannot be read",
                    dialog_id=dialog.dialog_id,
                    path=str(dialog.audio_path),
                    reason=str(exc),
                )
            if audio.sample_rate != dialog.sample_rate:
                from .errors import E_SAMPLE_RATE

                raise_error(
                    E_SAMPLE_RATE,
                    "audio sample rate differs from dialog manifest",
                    dialog_id=dialog.dialog_id,
                    expected=dialog.sample_rate,
                    actual=audio.sample_rate,
                )
            expected_frame_count = frame_count_for(
                audio.sample_count,
                self.config.feature,
                audio.sample_rate,
            )
            try:
                truth_rows = load_truth(dialog.truth_path, expected_frame_count)
            except BusinessError:
                raise
            except Exception as exc:
                raise_error(
                    E_INPUT_INVALID,
                    "truth input cannot be read",
                    dialog_id=dialog.dialog_id,
                    path=str(dialog.truth_path),
                    reason=str(exc),
                )
            truth_speakers = {int(row["speaker_id"]) for row in truth_rows}
            unknown_truth = sorted(truth_speakers - set(dialog.speaker_ids))
            if unknown_truth:
                raise_error(
                    E_SPEAKER_UNKNOWN,
                    "truth contains a speaker outside the dialog declaration",
                    dialog_id=dialog.dialog_id,
                    speaker_ids=unknown_truth,
                    declared=list(dialog.speaker_ids),
                )
            truth_version = truth_semantic_hash(truth_rows)
            truth_hashes = [truth_frame_hash(row) for row in truth_rows]
            dialog_enrollment_version = dataset.enrollment_version_for(
                dialog.speaker_ids
            )
            context_versions = self._context_versions(
                enrollment_version=dialog_enrollment_version,
                dataset_enrollment_version=dataset.enrollment_version,
                truth_version=truth_version,
                config_version=config_version,
            )

            if expected_frame_count > self.config.resources.max_frames_per_analysis:
                if self.config.resources.on_limit == "error":
                    resource_limit_error(
                        "max_frames_per_analysis",
                        dialog_id=dialog.dialog_id,
                        frame_count=expected_frame_count,
                        limit=self.config.resources.max_frames_per_analysis,
                    )
                degraded_categories.add("frame_budget_soft_limit")

            dtype = self.config.feature.dtype
            feature_bytes = check_feature_memory(
                expected_frame_count,
                len(("energy", "zcr", "centroid", "rolloff")),
                dtype,
                self.config.resources.max_feature_bytes,
            )
            if feature_bytes > self.config.resources.max_feature_bytes:
                if self.config.resources.on_limit == "error":
                    resource_limit_error(
                        "max_feature_bytes",
                        dialog_id=dialog.dialog_id,
                        required_bytes=feature_bytes,
                        limit=self.config.resources.max_feature_bytes,
                        dtype=dtype,
                    )
                if dtype == "float64":
                    dtype = "float32"
                    feature_bytes = check_feature_memory(
                        expected_frame_count, 4, dtype, self.config.resources.max_feature_bytes
                    )
                    degraded_categories.add("feature_precision_float32")
                if feature_bytes > self.config.resources.max_feature_bytes:
                    resource_limit_error(
                        "max_feature_bytes",
                        dialog_id=dialog.dialog_id,
                        required_bytes=feature_bytes,
                        limit=self.config.resources.max_feature_bytes,
                        dtype=dtype,
                    )

            identity = audio_identity(audio)
            fkey = feature_key(
                dialog_id=dialog.dialog_id,
                audio_identity=identity,
                config=self.config.feature,
                dtype=dtype,
            )
            feature_entry = None if force_full_recompute else self.cache.get_optional(fkey.cache_key)
            feature_reused = feature_entry is not None
            if feature_entry is not None:
                feature_result = FeatureResult.from_payload(feature_entry.payload)
                if feature_result.frame_count != expected_frame_count:
                    raise_error(
                        E_FRAME_COUNT,
                        "cached feature frame count differs from current frame plan",
                        dialog_id=dialog.dialog_id,
                        expected=expected_frame_count,
                        actual=feature_result.frame_count,
                    )
            else:
                feature_result = extract_features(
                    audio.samples,
                    self.config.feature,
                    sample_rate=audio.sample_rate,
                    output_dtype=dtype,
                )
                if feature_result.frame_count != expected_frame_count:
                    raise_error(
                        E_FRAME_COUNT,
                        "feature extractor did not produce the planned frame count",
                        dialog_id=dialog.dialog_id,
                        expected=expected_frame_count,
                        actual=feature_result.frame_count,
                    )
                feature_entry = self._cache_put(
                    key=fkey,
                    node="features",
                    context_versions=context_versions,
                    payload=feature_result.to_payload(),
                    degraded_categories=degraded_categories,
                )
            self._add_status(
                all_statuses,
                node="features",
                dialog_id=dialog.dialog_id,
                cache_key=fkey.cache_key,
                reused=feature_reused,
                entry=feature_entry,
            )

            akey = assignment_key(
                dialog_id=dialog.dialog_id,
                speaker_ids=dialog.speaker_ids,
                feature_key_value=fkey.cache_key,
                feature_hash=feature_result.output_hash,
                enrollment_version=dialog_enrollment_version,
                feature_config=self.config.feature,
                speaker_config=self.config.speaker,
            )
            assignment_entry = (
                None if force_full_recompute else self.cache.get_optional(akey.cache_key)
            )
            assignment_reused = assignment_entry is not None
            if assignment_entry is not None:
                assignment_result = AssignmentResult.from_payload(assignment_entry.payload)
            else:
                assignment_result, _ = assign_speakers(
                    feature_result,
                    registry,
                    dialog.speaker_ids,
                    self.config.speaker,
                    frame_samples=self.config.feature.frame_samples(audio.sample_rate),
                    sample_rate=audio.sample_rate,
                )
                assignment_entry = self._cache_put(
                    key=akey,
                    node="assignment",
                    context_versions=context_versions,
                    payload=assignment_result.to_payload(),
                    degraded_categories=degraded_categories,
                )
            if assignment_result.labels.shape[0] != expected_frame_count:
                raise_error(
                    E_FRAME_COUNT,
                    "assignment label count differs from feature frame count",
                    dialog_id=dialog.dialog_id,
                    expected=expected_frame_count,
                    actual=int(assignment_result.labels.shape[0]),
                )
            invalid_labels = sorted(
                set(int(item) for item in assignment_result.labels.tolist())
                - set(dialog.speaker_ids)
            )
            if invalid_labels:
                raise_error(
                    E_SPEAKER_UNKNOWN,
                    "assigned labels are outside the dialog declaration",
                    dialog_id=dialog.dialog_id,
                    speaker_ids=invalid_labels,
                    declared=list(dialog.speaker_ids),
                )
            self._add_status(
                all_statuses,
                node="assignment",
                dialog_id=dialog.dialog_id,
                cache_key=akey.cache_key,
                reused=assignment_reused,
                entry=assignment_entry,
            )

            segment_details: list[dict[str, Any]] = []
            segment_entries: list[CacheEntry | None] = []
            segment_path_items: list[dict[str, Any]] = []
            segment_status_reused: dict[str, bool] = {}
            segment_record_ids: dict[str, str] = {}
            assignment_item = (
                _path_item(node="assignment", entry=assignment_entry, parents=[fkey.cache_key])
                if assignment_entry is not None
                else None
            )
            feature_item = (
                _path_item(node="features", entry=feature_entry, parents=[])
                if feature_entry is not None
                else None
            )
            for segment in assignment_result.segments:
                if segment.start_frame < 0 or segment.end_frame > expected_frame_count:
                    raise_error(
                        E_SPEAKER_UNKNOWN,
                        "segment boundary is outside the frame range",
                        dialog_id=dialog.dialog_id,
                        segment_id=segment.segment_id,
                    )
                slice_rows = truth_rows[segment.start_frame : segment.end_frame]
                slice_hash = _truth_slice_hash(slice_rows)
                skey = segment_key(
                    dialog_id=dialog.dialog_id,
                    segment_id=segment.segment_id,
                    start_frame=segment.start_frame,
                    end_frame=segment.end_frame,
                    speaker_id=segment.speaker_id,
                    assignment_key_value=akey.cache_key,
                    assignment_hash=assignment_result.output_hash,
                    truth_slice_hash=slice_hash,
                    enrollment_version=dialog_enrollment_version,
                    feature_config=self.config.feature,
                    speaker_config=self.config.speaker,
                    regression_config=self.config.regression,
                )
                segment_entry = (
                    None if force_full_recompute else self.cache.get_optional(skey.cache_key)
                )
                reused_segment = segment_entry is not None
                if segment_entry is not None:
                    detail = segment_entry.payload.get("segment")
                    if not isinstance(detail, dict):
                        raise_error(
                            E_CACHE_CORRUPT,
                            "segment cache payload is malformed",
                            cache_key=skey.cache_key,
                        )
                    if detail.get("output_hash") != segment_entry.payload.get("output_hash"):
                        raise_error(
                            E_CACHE_CORRUPT,
                            "segment cache output hash mismatch",
                            cache_key=skey.cache_key,
                        )
                    if (
                        detail.get("segment_id") != segment.segment_id
                        or int(detail.get("start_frame", -1)) != segment.start_frame
                        or int(detail.get("end_frame", -1)) != segment.end_frame
                    ):
                        raise_error(
                            E_INCONSISTENT,
                            "segment cache identity differs from planned segment",
                            cache_key=skey.cache_key,
                        )
                else:
                    detail = regress_segment(
                        feature_result.features,
                        slice_rows,
                        segment,
                        self.config.regression,
                        self.config.feature.frame_shift_ms,
                        slice_hash,
                    )
                    segment_entry = self._cache_put(
                        key=skey,
                        node="segment",
                        context_versions=context_versions,
                        payload={"segment": detail, "output_hash": detail["output_hash"]},
                        degraded_categories=degraded_categories,
                    )
                segment_details.append(detail)
                segment_entries.append(segment_entry)
                segment_status_reused[segment.segment_id] = reused_segment
                self._add_status(
                    all_statuses,
                    node="segment",
                    dialog_id=dialog.dialog_id,
                    cache_key=skey.cache_key,
                    reused=reused_segment,
                    entry=segment_entry,
                    segment_id=segment.segment_id,
                )
                if segment_entry is not None and assignment_item is not None:
                    segment_path_items.append(
                        _path_item(
                            node="segment",
                            entry=segment_entry,
                            parents=[akey.cache_key],
                            segment_id=segment.segment_id,
                        )
                    )

            segment_cache_keys = [
                segment_key(
                    dialog_id=dialog.dialog_id,
                    segment_id=segment.segment_id,
                    start_frame=segment.start_frame,
                    end_frame=segment.end_frame,
                    speaker_id=segment.speaker_id,
                    assignment_key_value=akey.cache_key,
                    assignment_hash=assignment_result.output_hash,
                    truth_slice_hash=_truth_slice_hash(
                        truth_rows[segment.start_frame : segment.end_frame]
                    ),
                    enrollment_version=dialog_enrollment_version,
                    feature_config=self.config.feature,
                    speaker_config=self.config.speaker,
                    regression_config=self.config.regression,
                ).cache_key
                for segment in assignment_result.segments
            ]
            segment_hashes = [detail["output_hash"] for detail in segment_details]
            dkey = dialog_key(
                dialog_id=dialog.dialog_id,
                segment_cache_keys=segment_cache_keys,
                segment_hashes=segment_hashes,
                truth_version=truth_version,
                enrollment_version=dialog_enrollment_version,
                feature_config=self.config.feature,
                speaker_config=self.config.speaker,
                regression_config=self.config.regression,
                aggregation_config=self.config.aggregation,
            )
            dialog_entry = (
                None if force_full_recompute else self.cache.get_optional(dkey.cache_key)
            )
            dialog_reused = dialog_entry is not None
            if dialog_entry is not None:
                cached_dialog = dialog_entry.payload
                if cached_dialog.get("output_hash") is None:
                    raise_error(
                        E_CACHE_CORRUPT,
                        "dialog cache payload has no output hash",
                        cache_key=dkey.cache_key,
                    )
                recomputed = aggregate_segments(segment_details, self.config.aggregation)
                if (
                    recomputed["output_hash"] != cached_dialog.get("output_hash")
                    or recomputed["scores"] != cached_dialog.get("scores")
                ):
                    raise_error(
                        E_CACHE_CORRUPT,
                        "dialog cache payload cannot be independently reproduced",
                        cache_key=dkey.cache_key,
                    )
                dialog_payload = cached_dialog
            else:
                dialog_payload = {
                    **aggregate_segments(segment_details, self.config.aggregation),
                    "aggregation_config": self.config.aggregation.to_dict(),
                }
                dialog_entry = self._cache_put(
                    key=dkey,
                    node="dialog",
                    context_versions=context_versions,
                    payload=dialog_payload,
                    degraded_categories=degraded_categories,
                )
            self._add_status(
                all_statuses,
                node="dialog",
                dialog_id=dialog.dialog_id,
                cache_key=dkey.cache_key,
                reused=dialog_reused,
                entry=dialog_entry,
            )

            dialog_path_item = (
                _path_item(
                    node="dialog",
                    entry=dialog_entry,
                    parents=segment_cache_keys,
                )
                if dialog_entry is not None
                else None
            )
            traceable = (
                feature_item is not None
                and assignment_item is not None
                and dialog_path_item is not None
                and all(entry is not None for entry in segment_entries)
            )
            if traceable:
                context_hash = hash_value(context_versions)[:16]
                for index, segment in enumerate(assignment_result.segments):
                    detail = segment_details[index]
                    terminal_key = segment_cache_keys[index]
                    record_id = make_lineage_id(
                        dialog.dialog_id,
                        "segment",
                        terminal_key,
                        segment.segment_id,
                        context_hash,
                    )
                    record = {
                        "schema": LINEAGE_SCHEMA,
                        "record_id": record_id,
                        "dialog_id": dialog.dialog_id,
                        "kind": "segment",
                        "segment_id": segment.segment_id,
                        "config_version": config_version,
                        "config_checksum": config_checksum,
                        "input_versions": context_versions,
                        "node_path": [feature_item, assignment_item, segment_path_items[index]],
                        "score": detail,
                    }
                    self.lineage.put(record)
                    segment_record_ids[segment.segment_id] = record_id
                dialog_record_id = make_lineage_id(
                    dialog.dialog_id,
                    "dialog",
                    dkey.cache_key,
                    None,
                    context_hash,
                )
                dialog_record = {
                    "schema": LINEAGE_SCHEMA,
                    "record_id": dialog_record_id,
                    "dialog_id": dialog.dialog_id,
                    "kind": "dialog",
                    "segment_id": None,
                    "config_version": config_version,
                    "config_checksum": config_checksum,
                    "input_versions": context_versions,
                    "node_path": [
                        feature_item,
                        assignment_item,
                        *segment_path_items,
                        dialog_path_item,
                    ],
                    "score": dialog_payload["scores"],
                }
                self.lineage.put(dialog_record)

            current_segment_keys = {
                segment.segment_id: key
                for segment, key in zip(assignment_result.segments, segment_cache_keys)
            }
            previous_dialog = previous_dialogs.get(dialog.dialog_id)
            previous_truth_hashes = (
                list(previous_dialog.get("truth_frame_hashes", []))
                if previous_dialog is not None
                else []
            )
            truth_changed_frames = [
                index
                for index in range(expected_frame_count)
                if index >= len(previous_truth_hashes)
                or previous_truth_hashes[index] != truth_hashes[index]
            ]
            feature_changed = (
                previous_dialog is None
                or previous_dialog["feature"]["cache_key"] != fkey.cache_key
            )
            assignment_changed = (
                previous_dialog is None
                or previous_dialog["assignment"]["cache_key"] != akey.cache_key
            )
            previous_segment_keys = (
                {
                    str(item["segment_id"]): str(item["cache_key"])
                    for item in previous_dialog.get("segment_keys", [])
                }
                if previous_dialog is not None
                else {}
            )
            changed_segment_ids = {
                segment_id
                for segment_id, key in current_segment_keys.items()
                if previous_segment_keys.get(segment_id) != key
            }
            if previous_dialog is None or feature_changed or assignment_changed:
                affected_frames = list(range(expected_frame_count))
                affected_segments = [
                    segment.segment_id for segment in assignment_result.segments
                ]
            else:
                affected_frames = sorted(set(truth_changed_frames))
                for segment in assignment_result.segments:
                    if any(
                        segment.start_frame <= frame < segment.end_frame
                        for frame in affected_frames
                    ):
                        changed_segment_ids.add(segment.segment_id)
                affected_segments = sorted(changed_segment_ids)
                for segment_id in changed_segment_ids:
                    for segment in assignment_result.segments:
                        if segment.segment_id == segment_id:
                            affected_frames.extend(
                                range(segment.start_frame, segment.end_frame)
                            )
                affected_frames = sorted(set(affected_frames))
            affected_frames_report[dialog.dialog_id] = affected_frames
            affected_segments_report[dialog.dialog_id] = sorted(set(affected_segments))
            dialog_changed = (
                previous_dialog is None
                or previous_dialog["dialog"]["cache_key"] != dkey.cache_key
            )
            if previous_dialog is None or any(
                [
                    feature_changed,
                    assignment_changed,
                    bool(changed_segment_ids),
                    dialog_changed,
                ]
            ):
                changed_dialog_ids.append(dialog.dialog_id)
            if previous_dialog is None:
                changed_node_ids.extend(
                    [
                        _node_id(dialog.dialog_id, "features"),
                        _node_id(dialog.dialog_id, "assignment"),
                        *[
                            _node_id(dialog.dialog_id, "segment", segment.segment_id)
                            for segment in assignment_result.segments
                        ],
                        _node_id(dialog.dialog_id, "dialog"),
                    ]
                )
            else:
                if feature_changed:
                    changed_node_ids.append(_node_id(dialog.dialog_id, "features"))
                if assignment_changed:
                    changed_node_ids.append(_node_id(dialog.dialog_id, "assignment"))
                changed_node_ids.extend(
                    _node_id(dialog.dialog_id, "segment", segment_id)
                    for segment_id in changed_segment_ids
                )
                if dialog_changed:
                    changed_node_ids.append(_node_id(dialog.dialog_id, "dialog"))

            dialogs_out.append(
                {
                    "dialog_id": dialog.dialog_id,
                    "sample_rate": audio.sample_rate,
                    "duration_ms": int(round(audio.sample_count * 1000.0 / audio.sample_rate)),
                    "seed": dialog.seed,
                    "speaker_ids": list(dialog.speaker_ids),
                    "enrollment_version": dataset.enrollment_version,
                    "dialog_enrollment_version": dialog_enrollment_version,
                    "truth_version": truth_version,
                    "truth_frame_hashes": truth_hashes,
                    "audio_identity": identity,
                    "feature": {
                        "cache_key": fkey.cache_key,
                        "output_hash": feature_result.output_hash,
                        "frame_count": feature_result.frame_count,
                        "frame_indices": feature_result.frame_indices.tolist(),
                        "frame_starts": feature_result.frame_starts.tolist(),
                        "feature_names": list(feature_result.feature_names),
                        "dtype": feature_result.dtype,
                    },
                    "assignment": {
                        "cache_key": akey.cache_key,
                        "output_hash": assignment_result.output_hash,
                        "frame_indices": feature_result.frame_indices.tolist(),
                        "labels": assignment_result.labels.tolist(),
                        "segments": [
                            segment.to_dict() for segment in assignment_result.segments
                        ],
                    },
                    "segment_keys": [
                        {"segment_id": segment.segment_id, "cache_key": key}
                        for segment, key in zip(
                            assignment_result.segments, segment_cache_keys
                        )
                    ],
                    "segments": segment_details,
                    "dialog": {
                        "cache_key": dkey.cache_key,
                        "output_hash": dialog_payload["output_hash"],
                        "scores": dialog_payload["scores"],
                        "aggregation": dialog_payload["aggregation"],
                        "aggregation_config": dialog_payload.get(
                            "aggregation_config", self.config.aggregation.to_dict()
                        ),
                    },
                    "lineage": {
                        "traceable": traceable,
                        "segment_record_ids": segment_record_ids,
                        "dialog_record_id": (
                            dialog_record_id if traceable else None
                        ),
                    },
                    "node_versions": {
                        "features": feature_entry.version if feature_entry else None,
                        "assignment": assignment_entry.version
                        if assignment_entry
                        else None,
                        "segments": {
                            segment.segment_id: entry.version if entry else None
                            for segment, entry in zip(
                                assignment_result.segments, segment_entries
                            )
                        },
                        "dialog": dialog_entry.version if dialog_entry else None,
                    },
                }
            )

        dialogs_out.sort(key=lambda item: item["dialog_id"])
        reused_nodes = sorted(
            [item for item in all_statuses if item["reused"]],
            key=lambda item: item["node_id"],
        )
        recomputed_nodes = sorted(
            [item for item in all_statuses if not item["reused"]],
            key=lambda item: item["node_id"],
        )
        affected_node_ids = sorted(item["node_id"] for item in all_statuses)
        ranking = [
            {
                "rank": rank,
                "dialog_id": item["dialog_id"],
                "valence": item["dialog"]["scores"]["valence"]["score"],
                "arousal": item["dialog"]["scores"]["arousal"]["score"],
            }
            for rank, item in enumerate(
                sorted(
                    dialogs_out,
                    key=lambda item: (
                        -float(item["dialog"]["scores"]["valence"]["score"]),
                        item["dialog_id"],
                    ),
                ),
                start=1,
            )
        ]
        report = {
            "reused_nodes": reused_nodes,
            "recomputed_nodes": recomputed_nodes,
            "affected_node_ids": affected_node_ids,
            "affected_frames": affected_frames_report,
            "affected_segments": affected_segments_report,
            "changed_dialog_ids": sorted(set(changed_dialog_ids)),
            "changed_node_ids": sorted(set(changed_node_ids)),
            "degraded_categories": sorted(degraded_categories),
            "force_full_recompute": bool(force_full_recompute),
        }
        fingerprint = self._result_fingerprint(dialogs_out)
        data: dict[str, Any] = {
            "schema": RUN_SCHEMA,
            "config_version": config_version,
            "config_checksum": config_checksum,
            "pipeline_config": self.config.to_dict(),
            "dataset": {
                "enrollment_version": dataset.enrollment_version,
                "dialog_count": len(dataset.dialogs),
                "enrollment_count": len(dataset.enrollments),
            },
            "dialogs": dialogs_out,
            "ranking": ranking,
            "report": report,
            "degraded_categories": sorted(degraded_categories),
            "result_fingerprint": fingerprint,
        }
        validate_run_consistency(data)
        run_id = "run-" + hash_value(
            {
                "result_fingerprint": fingerprint,
                "report": report,
                "config_version": config_version,
            }
        )[:32]
        data["run_id"] = run_id
        self._persist_run(data)
        return RunResult(data=data)

    def _persist_run(self, data: dict[str, Any]) -> None:
        run_id = str(data["run_id"])
        path = self._run_path(run_id)
        body = {
            "schema": RUN_RECORD_SCHEMA,
            "result": data,
            "checksum": hash_value(data),
        }
        if path.exists():
            existing = read_json(path)
            if (
                not isinstance(existing, dict)
                or existing.get("checksum") != hash_value(existing.get("result"))
                or existing.get("result", {}).get("run_id") != run_id
            ):
                raise_error(
                    E_CACHE_CORRUPT,
                    "persisted run record is corrupt or collides",
                    run_id=run_id,
                    path=str(path),
                )
            return
        write_json_atomic(path, body)

    def load_run(self, run_id: str) -> RunResult:
        path = self._run_path(run_id)
        if not path.exists():
            raise BusinessError("E_RUN_MISSING", "run id does not exist", run_id=run_id)
        try:
            body = read_json(path)
        except Exception as exc:
            raise_error(
                E_CACHE_CORRUPT,
                "run record cannot be parsed",
                run_id=run_id,
                path=str(path),
                reason=str(exc),
            )
        if (
            not isinstance(body, dict)
            or body.get("schema") != RUN_RECORD_SCHEMA
            or body.get("checksum") != hash_value(body.get("result"))
        ):
            raise_error(
                E_CACHE_CORRUPT,
                "run record checksum mismatch",
                run_id=run_id,
                path=str(path),
            )
        result = body["result"]
        if result.get("run_id") != run_id:
            raise_error(
                E_CACHE_CORRUPT,
                "run record id mismatch",
                run_id=run_id,
                path=str(path),
            )
        return RunResult(data=result)

    def self_check(self, run: RunResult | Mapping[str, Any]) -> dict[str, Any]:
        data = run.data if isinstance(run, RunResult) else dict(run)
        return validate_run_consistency(data)

    def trace_score(self, record_id: str) -> dict[str, Any]:
        try:
            record = self.lineage.get(record_id)
        except BusinessError as exc:
            raise_error(
                E_LINEAGE_BROKEN,
                "lineage record cannot be loaded",
                record_id=record_id,
                broken_node="lineage-record",
                version=None,
                reason=str(exc),
                cause=exc.to_dict(),
            )
        try:
            return replay_lineage(record, self.cache, self.config_store)
        except BusinessError as exc:
            if exc.code == E_LINEAGE_BROKEN:
                raise
            raise_error(
                E_LINEAGE_BROKEN,
                "lineage replay failed",
                record_id=record_id,
                broken_node="unknown",
                version=None,
                reason=str(exc),
                cause=exc.to_dict(),
            )

    def list_scores(self, dialog_id: str) -> list[dict[str, Any]]:
        records = self.lineage.list_for_dialog(dialog_id)
        summaries = []
        for record in records:
            summaries.append(
                {
                    "record_id": record["record_id"],
                    "dialog_id": record["dialog_id"],
                    "kind": record["kind"],
                    "segment_id": record.get("segment_id"),
                    "config_version": record.get("config_version"),
                    "input_versions": record.get("input_versions", {}),
                    "terminal_cache_key": record["node_path"][-1]["cache_key"],
                    "terminal_version": record["node_path"][-1]["version"],
                }
            )
        return summaries


def validate_run_consistency(data: Mapping[str, Any]) -> dict[str, Any]:
    if data.get("schema") != RUN_SCHEMA:
        raise_error(
            E_INCONSISTENT,
            "run data has an unexpected schema",
            schema=data.get("schema"),
        )
    dialogs = list(data.get("dialogs", []))
    if not dialogs:
        raise_error(E_INCONSISTENT, "run contains no dialogs")
    expected_ids: set[str] = set()
    for dialog in dialogs:
        dialog_id = str(dialog["dialog_id"])
        frame_count = int(dialog["feature"]["frame_count"])
        frame_indices = [int(item) for item in dialog["feature"]["frame_indices"]]
        labels = [int(item) for item in dialog["assignment"]["labels"]]
        assignment_indices = [
            int(item) for item in dialog["assignment"].get("frame_indices", [])
        ]
        truth_hashes = list(dialog.get("truth_frame_hashes", []))
        if frame_indices != list(range(frame_count)):
            raise_error(
                E_INCONSISTENT,
                "feature frame_index set is not contiguous",
                dialog_id=dialog_id,
                frame_count=frame_count,
            )
        if len(labels) != frame_count:
            raise_error(
                E_INCONSISTENT,
                "assignment label count differs from feature frame count",
                dialog_id=dialog_id,
                labels=len(labels),
                features=frame_count,
            )
        if assignment_indices != frame_indices:
            raise_error(
                E_INCONSISTENT,
                "assignment frame_index set differs from feature frame_index set",
                dialog_id=dialog_id,
            )
        if len(truth_hashes) != frame_count:
            raise_error(
                E_INCONSISTENT,
                "truth frame count differs from feature frame count",
                dialog_id=dialog_id,
                truth=len(truth_hashes),
                features=frame_count,
            )
        cursor = 0
        segment_details = list(dialog.get("segments", []))
        for segment in segment_details:
            start = int(segment["start_frame"])
            end = int(segment["end_frame"])
            if start != cursor or end <= start or end > frame_count:
                raise_error(
                    E_INCONSISTENT,
                    "segment frame ranges do not partition the dialog",
                    dialog_id=dialog_id,
                    segment_id=segment.get("segment_id"),
                    start=start,
                    end=end,
                    expected_start=cursor,
                )
            if int(segment["frame_count"]) != end - start:
                raise_error(
                    E_INCONSISTENT,
                    "segment frame_count is inconsistent with range",
                    dialog_id=dialog_id,
                    segment_id=segment.get("segment_id"),
                )
            cursor = end
        if cursor != frame_count:
            raise_error(
                E_INCONSISTENT,
                "segments do not cover every frame",
                dialog_id=dialog_id,
                covered=cursor,
                frame_count=frame_count,
            )
        assignment_segments = list(dialog["assignment"]["segments"])
        if len(assignment_segments) != len(segment_details):
            raise_error(
                E_INCONSISTENT,
                "segment count differs between assignment and regression",
                dialog_id=dialog_id,
            )
        for assignment_segment, detail in zip(assignment_segments, segment_details):
            for field in ("segment_id", "start_frame", "end_frame", "speaker_id"):
                if assignment_segment.get(field) != detail.get(field):
                    raise_error(
                        E_INCONSISTENT,
                        "assignment and regression segment metadata differ",
                        dialog_id=dialog_id,
                        segment_id=detail.get("segment_id"),
                        field=field,
                    )
        aggregation_config = AggregationConfig(**dialog["dialog"]["aggregation_config"])
        recomputed = aggregate_segments(segment_details, aggregation_config)
        if (
            recomputed["output_hash"] != dialog["dialog"]["output_hash"]
            or recomputed["scores"] != dialog["dialog"]["scores"]
        ):
            raise_error(
                E_INCONSISTENT,
                "dialog aggregate cannot be reconstructed from segment details",
                dialog_id=dialog_id,
            )
        expected_ids.update(
            {
                _node_id(dialog_id, "features"),
                _node_id(dialog_id, "assignment"),
                *[
                    _node_id(dialog_id, "segment", str(segment["segment_id"]))
                    for segment in segment_details
                ],
                _node_id(dialog_id, "dialog"),
            }
        )
    report = data.get("report")
    if not isinstance(report, dict):
        raise_error(E_INCONSISTENT, "run report is missing")
    reused = list(report.get("reused_nodes", []))
    recomputed = list(report.get("recomputed_nodes", []))
    reused_ids = [str(item["node_id"]) for item in reused]
    recomputed_ids = [str(item["node_id"]) for item in recomputed]
    reused_set = set(reused_ids)
    recomputed_set = set(recomputed_ids)
    if len(reused_ids) != len(reused_set) or len(recomputed_ids) != len(recomputed_set):
        raise_error(E_INCONSISTENT, "duplicate node ids in reuse/recompute report")
    if reused_set & recomputed_set:
        raise_error(
            E_INCONSISTENT,
            "reuse and recompute sets overlap",
            overlap=sorted(reused_set & recomputed_set),
        )
    union = reused_set | recomputed_set
    if union != expected_ids:
        raise_error(
            E_INCONSISTENT,
            "reuse/recompute union does not cover the node universe",
            missing=sorted(expected_ids - union),
            extra=sorted(union - expected_ids),
        )
    affected_ids = set(str(item) for item in report.get("affected_node_ids", []))
    if affected_ids != expected_ids:
        raise_error(
            E_INCONSISTENT,
            "affected node set is inconsistent",
            missing=sorted(expected_ids - affected_ids),
            extra=sorted(affected_ids - expected_ids),
        )
    affected_frames_map = report.get("affected_frames")
    affected_segments_map = report.get("affected_segments")
    if not isinstance(affected_frames_map, dict) or not isinstance(affected_segments_map, dict):
        raise_error(
            E_INCONSISTENT,
            "affected frame/segment maps are missing",
        )
    for dialog in dialogs:
        dialog_id = str(dialog["dialog_id"])
        frame_count = int(dialog["feature"]["frame_count"])
        if dialog_id not in affected_frames_map or dialog_id not in affected_segments_map:
            raise_error(
                E_INCONSISTENT,
                "dialog is missing from affected frame/segment maps",
                dialog_id=dialog_id,
            )
        frames = [int(item) for item in affected_frames_map[dialog_id]]
        if frames != sorted(set(frames)) or any(item < 0 or item >= frame_count for item in frames):
            raise_error(
                E_INCONSISTENT,
                "affected frame set is invalid",
                dialog_id=dialog_id,
            )
        valid_segments = {str(item["segment_id"]) for item in dialog["segments"]}
        segment_id_set = set(
            str(item) for item in report["affected_segments"].get(dialog_id, [])
        )
        if not segment_id_set <= valid_segments:
            raise_error(
                E_INCONSISTENT,
                "affected segment set contains an unknown segment",
                dialog_id=dialog_id,
                unknown=sorted(segment_id_set - valid_segments),
            )
    return {
        "status": "ok",
        "dialog_count": len(dialogs),
        "node_count": len(expected_ids),
        "reused_count": len(reused_set),
        "recomputed_count": len(recomputed_set),
    }
