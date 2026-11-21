"""DAG execution, incremental invalidation, and snapshot orchestration."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .audio import audio_info
from .cache import Artifact, SnapshotStore, make_artifact, make_cache_key
from .config import (
    AppConfig,
    apply_runtime_identity,
    load_config,
    load_runtime_config,
    write_runtime_config,
)
from .errors import BusinessError, fail
from .models import Annotation, AudioInfo, Clip
from .nodes import (
    ENVELOPE_VERSION,
    EXPORT_VERSION,
    REFINE_VERSION,
    SPEAKER_VERSION,
    VAD_VERSION,
    compute_envelope,
    compute_export,
    compute_refine,
    compute_speaker,
    compute_vad,
)
from .nodes.envelope import envelope_key_details
from .nodes.export import export_key_details
from .nodes.refine import refine_key_details
from .nodes.speaker import speaker_key_details
from .nodes.vad import vad_key_details
from .replay import ReplayStore
from .samples import (
    annotation_hash,
    annotation_revision,
    load_annotations,
    load_clip_manifest,
    write_annotations,
)
from .util import ms_to_sample, sample_to_ms, sha256_value


ALL_NODES = ("envelope", "vad", "refine", "speaker", "export")


@dataclass(frozen=True)
class Change:
    kind: str
    details: dict[str, Any]


@dataclass
class RunResult:
    snapshot_version: int
    snapshot_replaced: bool
    node_statuses: dict[str, dict[str, Any]]
    recomputed_nodes: list[str]
    reused_nodes: list[str]
    affected_nodes: list[str]
    affected_ranges: list[dict[str, Any]]
    interval_recompute_count: int
    interval_reuse_count: int
    export_cache_key: str
    export_jsonl: str
    export_jsonl_sha256: str
    export_item_count: int
    self_check: dict[str, Any]
    replay_record_checksum: str

    def to_dict(self, include_jsonl: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": True,
            "snapshot_version": self.snapshot_version,
            "snapshot_replaced": self.snapshot_replaced,
            "node_statuses": self.node_statuses,
            "recomputed_nodes": self.recomputed_nodes,
            "reused_nodes": self.reused_nodes,
            "affected_nodes": self.affected_nodes,
            "affected_ranges": self.affected_ranges,
            "interval_recompute_count": self.interval_recompute_count,
            "interval_reuse_count": self.interval_reuse_count,
            "export_cache_key": self.export_cache_key,
            "export_jsonl_sha256": self.export_jsonl_sha256,
            "export_item_count": self.export_item_count,
            "self_check": self.self_check,
            "replay_record_checksum": self.replay_record_checksum,
        }
        if include_jsonl:
            data["export_jsonl"] = self.export_jsonl
        return data


def _validate_frame_values(config: AppConfig, frame_ms: float, hop_ms: float) -> None:
    if not math.isfinite(frame_ms) or frame_ms <= 0 or not math.isfinite(hop_ms) or hop_ms <= 0:
        fail(
            "E_FRAME_MS",
            "frame_ms and frame_hop_ms must be positive finite numbers",
            frame_ms=frame_ms,
            frame_hop_ms=hop_ms,
        )
    frame_samples = max(1, int(round(config.sample_rate * frame_ms / 1000.0)))
    hop_samples = max(1, int(round(config.sample_rate * hop_ms / 1000.0)))
    if hop_samples > frame_samples:
        fail(
            "E_FRAME_SHIFT",
            "frame hop must not exceed frame length",
            frame_ms=frame_ms,
            frame_hop_ms=hop_ms,
            frame_samples=frame_samples,
            hop_samples=hop_samples,
        )


class AnalysisSession:
    def __init__(
        self,
        config: AppConfig,
        *,
        use_runtime_annotations: bool = True,
        use_runtime_config: bool = True,
    ):
        runtime_identity = (
            load_runtime_config(config.paths.runtime_config)
            if use_runtime_config
            else None
        )
        if runtime_identity is not None and int(
            runtime_identity.get("config_version", 0)
        ) >= config.config_version:
            config = apply_runtime_identity(config, runtime_identity)
        self.config = config
        self.clips_list = load_clip_manifest(
            config.paths.samples_manifest, config.sample_rate
        )
        self.clips: dict[str, Clip] = {clip.clip_id: clip for clip in self.clips_list}
        annotation_path = config.paths.annotations_manifest
        if use_runtime_annotations and config.paths.runtime_annotations.exists():
            annotation_path = config.paths.runtime_annotations
        self.annotations = load_annotations(annotation_path, self.clips_list)
        self.audio_by_clip: dict[str, AudioInfo] = {
            clip.clip_id: audio_info(
                clip,
                Path(config.paths.samples_manifest).parent,
                config.resources.audio_chunk_samples,
            )
            for clip in self.clips_list
        }
        self.audio_manifest_hash = sha256_value(
            {
                clip_id: info.chunk_manifest_hash
                for clip_id, info in sorted(self.audio_by_clip.items())
            }
        )
        self.snapshots = SnapshotStore(
            config.paths.snapshots_dir, config.resources.max_snapshot_entries
        )
        self.replay = ReplayStore(config.paths.replay_dir, self.snapshots)
        self.artifacts: dict[str, Artifact] | None = None
        self.last_result: RunResult | None = None
        self.last_error: dict[str, Any] | None = None

    @classmethod
    def from_config_file(
        cls, path: str | Path, *, use_runtime_annotations: bool = True
    ) -> "AnalysisSession":
        return cls(
            load_config(path), use_runtime_annotations=use_runtime_annotations
        )

    def _record_error(self, error: BusinessError) -> None:
        self.last_error = error.to_dict()

    def _annotation_digest(self, annotations: list[Annotation]) -> tuple[str, int]:
        return annotation_hash(annotations), annotation_revision(annotations)

    def _affected_ranges(
        self, change: Change, annotations: list[Annotation]
    ) -> list[dict[str, Any]]:
        if change.kind == "annotation":
            segment_id = change.details["segment_id"]
            changed = next(
                (
                    annotation
                    for annotation in annotations
                    if annotation.segment_id == segment_id
                ),
                None,
            )
            if changed is None:
                fail(
                    "E_SEGMENT_UNKNOWN",
                    "corrected annotation no longer exists",
                    segment_id=segment_id,
                )
            start_sample = max(
                0, ms_to_sample(changed.start_ms, self.config.sample_rate)
            )
            end_sample = min(
                self.clips[changed.clip_id].expected_samples,
                ms_to_sample(changed.end_ms, self.config.sample_rate),
            )
            return [
                {
                    "clip_id": changed.clip_id,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "start_ms": sample_to_ms(start_sample, self.config.sample_rate),
                    "end_ms": sample_to_ms(end_sample, self.config.sample_rate),
                    "reason": f"annotation:{segment_id}",
                }
            ]
        if change.kind in {"noise_quantile", "frame"}:
            reason = f"{change.kind}:{change.details.get('version', '')}"
            return [
                {
                    "clip_id": clip.clip_id,
                    "start_sample": 0,
                    "end_sample": clip.expected_samples,
                    "start_ms": 0.0,
                    "end_ms": clip.duration_ms,
                    "reason": reason,
                }
                for clip in self.clips_list
            ]
        return []

    def _affected_nodes(self, change: Change) -> set[str]:
        if change.kind == "annotation":
            return {"speaker", "export"}
        if change.kind == "noise_quantile":
            return {"vad", "refine", "speaker", "export"}
        if change.kind == "frame":
            return set(ALL_NODES)
        return set()

    def _cache_or_compute(
        self,
        node: str,
        node_version: str,
        key_details: dict[str, Any],
        upstream_hash: str | None,
        compute: Callable[[], dict[str, Any]],
    ) -> tuple[Artifact, bool]:
        cache_key = make_cache_key(node, node_version, key_details)
        cached = self.snapshots.load_any(cache_key)
        if cached is not None:
            if (
                cached.node != node
                or cached.node_version != node_version
                or cached.upstream_hash != upstream_hash
            ):
                fail(
                    "E_CACHE_CORRUPT",
                    "cached artifact metadata does not match the requested node",
                    entry=cache_key,
                    version=self.snapshots.current_version(),
                )
            return cached, True
        return (
            make_artifact(
                node,
                node_version,
                key_details,
                compute(),
                upstream_hash,
            ),
            False,
        )

    def _execute(
        self,
        config: AppConfig,
        annotations: list[Annotation],
        change: Change,
        *,
        verify_full_recompute: bool = True,
    ) -> RunResult:
        affected_ranges = self._affected_ranges(change, annotations)
        affected_nodes = self._affected_nodes(change)
        digest, revision = self._annotation_digest(annotations)
        hit: dict[str, bool] = {}
        artifacts: dict[str, Artifact] = {}

        envelope_details = envelope_key_details(self.audio_by_clip, config)
        envelope, hit["envelope"] = self._cache_or_compute(
            "envelope",
            ENVELOPE_VERSION,
            envelope_details,
            None,
            lambda: compute_envelope(self.audio_by_clip, config),
        )
        artifacts["envelope"] = envelope

        vad_details = vad_key_details(envelope.payload, envelope.content_hash, config)
        vad, hit["vad"] = self._cache_or_compute(
            "vad",
            VAD_VERSION,
            vad_details,
            envelope.content_hash,
            lambda: compute_vad(envelope.payload, config),
        )
        artifacts["vad"] = vad

        refine_details = refine_key_details(vad.payload, vad.content_hash, config)
        refine, hit["refine"] = self._cache_or_compute(
            "refine",
            REFINE_VERSION,
            refine_details,
            vad.content_hash,
            lambda: compute_refine(envelope.payload, vad.payload, config),
        )
        artifacts["refine"] = refine

        speaker_details = speaker_key_details(
            refine.payload, refine.content_hash, config, digest, revision
        )
        previous_speaker = (
            self.artifacts.get("speaker")
            if self.artifacts is not None and change.kind == "annotation"
            else None
        )
        speaker, hit["speaker"] = self._cache_or_compute(
            "speaker",
            SPEAKER_VERSION,
            speaker_details,
            refine.content_hash,
            lambda: compute_speaker(
                envelope.payload,
                refine.payload,
                annotations,
                config,
                affected_ranges=affected_ranges,
                previous_payload=previous_speaker.payload if previous_speaker else None,
            ),
        )
        artifacts["speaker"] = speaker

        export_details = export_key_details(
            speaker.payload,
            speaker.content_hash,
            config,
            digest,
            revision,
            self.audio_manifest_hash,
        )
        export, hit["export"] = self._cache_or_compute(
            "export",
            EXPORT_VERSION,
            export_details,
            speaker.content_hash,
            lambda: compute_export(speaker.payload, self.clips, config),
        )
        artifacts["export"] = export

        if verify_full_recompute:
            fresh_export = self._compute_fresh_export(config, annotations)
            if fresh_export["jsonl"] != export.payload["jsonl"]:
                first = self._first_interval_difference(
                    export.payload["items"], fresh_export["items"]
                )
                fail(
                    "E_INCONSISTENT",
                    "incremental export differs from a full recomputation",
                    **first,
                )
            if fresh_export["jsonl_sha256"] != export.payload["jsonl_sha256"]:
                fail(
                    "E_INCONSISTENT",
                    "incremental export hash differs from a full recomputation",
                    incremental_sha256=export.payload["jsonl_sha256"],
                    full_sha256=fresh_export["jsonl_sha256"],
                )

        recomputed_nodes = [node for node in ALL_NODES if not hit[node]]
        reused_nodes = [node for node in ALL_NODES if hit[node]]
        self_check = self._self_check(
            artifacts,
            affected_nodes,
            set(recomputed_nodes),
            set(reused_nodes),
        )
        if not self_check["ok"]:
            fail(
                "E_INCONSISTENT",
                "cross-node self-check failed",
                checks=self_check,
            )

        snapshot_version, snapshot_replaced = self.snapshots.commit_generation(
            config.snapshot_identity(), artifacts.values()
        )
        record = self.replay.register(
            snapshot_version,
            artifacts,
            config,
            digest,
            revision,
            self.clips,
        )
        total_intervals = sum(
            len(clip["intervals"]) for clip in speaker.payload["clips"].values()
        )
        if hit["speaker"]:
            interval_recompute_count = 0
            interval_reuse_count = total_intervals
        else:
            speaker_stats = speaker.payload.get("stats", {})
            interval_recompute_count = int(
                speaker_stats.get("recomputed_intervals", total_intervals)
            )
            interval_reuse_count = int(speaker_stats.get("reused_intervals", 0))
        statuses = self._node_statuses(
            artifacts, hit, snapshot_version, affected_ranges
        )
        result = RunResult(
            snapshot_version=snapshot_version,
            snapshot_replaced=snapshot_replaced,
            node_statuses=statuses,
            recomputed_nodes=recomputed_nodes,
            reused_nodes=reused_nodes,
            affected_nodes=[node for node in ALL_NODES if node in affected_nodes],
            affected_ranges=affected_ranges,
            interval_recompute_count=interval_recompute_count,
            interval_reuse_count=interval_reuse_count,
            export_cache_key=export.cache_key,
            export_jsonl=export.payload["jsonl"],
            export_jsonl_sha256=export.payload["jsonl_sha256"],
            export_item_count=int(export.payload["item_count"]),
            self_check=self_check,
            replay_record_checksum=str(record["record_checksum"]),
        )
        self.artifacts = artifacts
        self.last_result = result
        self.last_error = None
        return result

    def _compute_fresh_export(
        self, config: AppConfig, annotations: list[Annotation]
    ) -> dict[str, Any]:
        envelope = compute_envelope(self.audio_by_clip, config)
        vad = compute_vad(envelope, config)
        refine = compute_refine(envelope, vad, config)
        speaker = compute_speaker(envelope, refine, annotations, config)
        return compute_export(speaker, self.clips, config)

    @staticmethod
    def _first_interval_difference(
        incremental: list[dict[str, Any]], full: list[dict[str, Any]]
    ) -> dict[str, Any]:
        for index, (left, right) in enumerate(zip(incremental, full)):
            if left != right:
                return {
                    "first_difference_index": index,
                    "segment_id": left.get("segment_id") or right.get("segment_id"),
                    "start_ms": left.get("start_ms", right.get("start_ms")),
                    "end_ms": left.get("end_ms", right.get("end_ms")),
                }
        index = min(len(incremental), len(full))
        item = (
            incremental[index]
            if index < len(incremental)
            else (full[index] if index < len(full) else {})
        )
        return {
            "first_difference_index": index,
            "segment_id": item.get("segment_id"),
            "start_ms": item.get("start_ms"),
            "end_ms": item.get("end_ms"),
        }

    def _self_check(
        self,
        artifacts: dict[str, Artifact],
        affected_nodes: set[str],
        recomputed: set[str],
        reused: set[str],
    ) -> dict[str, Any]:
        envelope = artifacts["envelope"].payload
        frame_coverage_ok = True
        frame_issues: list[str] = []
        for clip_id in envelope["clip_order"]:
            clip_data = envelope["clips"][clip_id]
            starts = clip_data["frame_start_samples"]
            ends = clip_data["frame_end_samples"]
            if not starts or starts[0] != 0 or ends[-1] != clip_data["total_samples"]:
                frame_coverage_ok = False
                frame_issues.append(f"{clip_id}: boundary")
                continue
            for index in range(1, len(starts)):
                if starts[index] > ends[index - 1]:
                    frame_coverage_ok = False
                    frame_issues.append(f"{clip_id}: gap at {starts[index]}")
                    break

        export = artifacts["export"].payload
        interval_no_overlap = True
        interval_issues: list[str] = []
        by_clip: dict[str, list[dict[str, Any]]] = {}
        for item in export["items"]:
            by_clip.setdefault(item["clip_id"], []).append(item)
        for clip_id, items in by_clip.items():
            previous_end = -1
            for item in sorted(items, key=lambda value: value["start_sample"]):
                if item["start_sample"] < previous_end:
                    interval_no_overlap = False
                    interval_issues.append(f"{clip_id}:{item['segment_id']}")
                    break
                previous_end = item["end_sample"]

        node_sets_disjoint = not (recomputed & reused)
        node_sets_cover_all = (recomputed | reused) == set(ALL_NODES)
        affected_covered = affected_nodes <= (recomputed | reused)
        ok = (
            frame_coverage_ok
            and interval_no_overlap
            and node_sets_disjoint
            and node_sets_cover_all
            and affected_covered
        )
        return {
            "ok": ok,
            "frame_coverage_without_gaps": frame_coverage_ok,
            "frame_issues": frame_issues,
            "intervals_non_overlapping": interval_no_overlap,
            "interval_issues": interval_issues,
            "reuse_recompute_disjoint": node_sets_disjoint,
            "node_sets_cover_graph": node_sets_cover_all,
            "affected_nodes_covered": affected_covered,
            "all_nodes": list(ALL_NODES),
            "recomputed_nodes": sorted(recomputed, key=ALL_NODES.index),
            "reused_nodes": sorted(reused, key=ALL_NODES.index),
            "affected_nodes": sorted(affected_nodes, key=ALL_NODES.index),
        }

    def _node_statuses(
        self,
        artifacts: dict[str, Artifact],
        hits: dict[str, bool],
        snapshot_version: int,
        affected_ranges: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        statuses: dict[str, dict[str, Any]] = {}
        for node in ALL_NODES:
            artifact = artifacts[node]
            payload = artifact.payload
            summary: dict[str, Any]
            if node == "envelope":
                summary = {
                    "frame_counts": {
                        clip_id: payload["clips"][clip_id]["frame_count"]
                        for clip_id in payload["clip_order"]
                    },
                    "total_frame_count": payload["total_frame_count"],
                }
            elif node == "vad":
                summary = {
                    "candidate_counts": {
                        clip_id: len(payload["clips"][clip_id]["intervals"])
                        for clip_id in payload["clip_order"]
                    }
                }
            elif node == "refine":
                summary = {
                    "interval_counts": {
                        clip_id: len(payload["clips"][clip_id]["intervals"])
                        for clip_id in payload["clip_order"]
                    }
                }
            elif node == "speaker":
                total_intervals = sum(
                    len(payload["clips"][clip_id]["intervals"])
                    for clip_id in payload["clip_order"]
                )
                summary = {
                    "interval_counts": {
                        clip_id: len(payload["clips"][clip_id]["intervals"])
                        for clip_id in payload["clip_order"]
                    },
                    "interval_recompute_count": (
                        0
                        if hits[node]
                        else payload.get("stats", {}).get(
                            "recomputed_intervals", total_intervals
                        )
                    ),
                    "interval_reuse_count": (
                        total_intervals
                        if hits[node]
                        else payload.get("stats", {}).get("reused_intervals", 0)
                    ),
                }
            else:
                summary = {
                    "item_count": payload["item_count"],
                    "jsonl_sha256": payload["jsonl_sha256"],
                }
            statuses[node] = {
                "status": "reused" if hits[node] else "recomputed",
                "cache_hit": hits[node],
                "cache_key": artifact.cache_key,
                "content_hash": artifact.content_hash,
                "upstream_hash": artifact.upstream_hash,
                "node_version": artifact.node_version,
                "snapshot_version": snapshot_version,
                "affected_range_count": len(affected_ranges),
                "summary": summary,
            }
        return statuses

    def analyze(self) -> RunResult:
        try:
            return self._execute(self.config, self.annotations, Change("analyze", {}))
        except BusinessError as error:
            self._record_error(error)
            raise

    def correct_annotation(
        self,
        segment_id: str,
        *,
        speaker_label: str | None = None,
        label_revision: int | None = None,
        confidence: float | None = None,
        confidence_lower: float | None = None,
        confidence_upper: float | None = None,
    ) -> RunResult:
        try:
            index = next(
                (
                    index
                    for index, annotation in enumerate(self.annotations)
                    if annotation.segment_id == segment_id
                ),
                None,
            )
            if index is None:
                fail(
                    "E_SEGMENT_UNKNOWN",
                    "segment_id is not present in the annotation manifest",
                    segment_id=segment_id,
                )
            old = self.annotations[index]
            global_revision = annotation_revision(self.annotations)
            new_revision = (
                max(old.label_revision, global_revision) + 1
                if label_revision is None
                else int(label_revision)
            )
            if new_revision < old.label_revision:
                fail(
                    "E_ANNOTATION_INVALID",
                    "label_revision cannot move backwards",
                    segment_id=segment_id,
                    old_revision=old.label_revision,
                    new_revision=new_revision,
                )
            if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
                fail(
                    "E_INTERVAL_INVALID",
                    "confidence must be in [0, 1]",
                    segment_id=segment_id,
                    confidence=confidence,
                )
            if (confidence_lower is None) != (confidence_upper is None):
                fail(
                    "E_INTERVAL_INVALID",
                    "confidence_lower and confidence_upper must be supplied together",
                    segment_id=segment_id,
                )
            if confidence_lower is not None and (
                float(confidence_lower) > float(confidence_upper)
            ):
                fail(
                    "E_INTERVAL_INVALID",
                    "confidence interval lower bound exceeds upper bound",
                    segment_id=segment_id,
                    confidence_lower=confidence_lower,
                    confidence_upper=confidence_upper,
                )
            updated_annotation = replace(
                old,
                speaker_label=(
                    old.speaker_label if speaker_label is None else str(speaker_label)
                ),
                label_revision=new_revision,
                confidence=old.confidence if confidence is None else float(confidence),
                confidence_lower=old.confidence_lower
                if confidence_lower is None
                else float(confidence_lower),
                confidence_upper=old.confidence_upper
                if confidence_upper is None
                else float(confidence_upper),
            )
            updated = list(self.annotations)
            updated[index] = updated_annotation
            updated.sort(
                key=lambda item: (item.clip_id, item.start_ms, item.end_ms, item.segment_id)
            )
            change = Change(
                "annotation",
                {
                    "segment_id": segment_id,
                    "old": old.to_dict(),
                    "new": updated_annotation.to_dict(),
                    "version": new_revision,
                },
            )
            result = self._execute(self.config, updated, change)
            self.annotations = updated
            write_annotations(self.config.paths.runtime_annotations, updated)
            return result
        except BusinessError as error:
            self._record_error(error)
            raise

    def set_noise_quantile(self, quantile: float) -> RunResult:
        try:
            value = float(quantile)
            if not math.isfinite(value) or not 0.0 <= value < 1.0:
                fail(
                    "E_CONFIG_INVALID",
                    "noise_quantile must be in [0, 1)",
                    value=quantile,
                )
            config = self.config.with_noise_quantile(value)
            change = Change(
                "noise_quantile",
                {
                    "old": self.config.vad.noise_quantile,
                    "new": value,
                    "version": config.vad.threshold_version,
                },
            )
            result = self._execute(config, self.annotations, change)
            self.config = config
            write_runtime_config(config)
            return result
        except BusinessError as error:
            self._record_error(error)
            raise

    def set_frame(self, frame_ms: float, frame_hop_ms: float) -> RunResult:
        try:
            _validate_frame_values(self.config, frame_ms, frame_hop_ms)
            config = self.config.with_frame(frame_ms, frame_hop_ms)
            change = Change(
                "frame",
                {
                    "old": [self.config.frame_ms, self.config.frame_hop_ms],
                    "new": [frame_ms, frame_hop_ms],
                    "version": f"{frame_ms}/{frame_hop_ms}",
                },
            )
            result = self._execute(config, self.annotations, change)
            self.config = config
            write_runtime_config(config)
            return result
        except BusinessError as error:
            self._record_error(error)
            raise

    def replay_export(self, cache_key: str | None = None):
        try:
            key = cache_key
            if key is None:
                latest = self.replay.latest_record()
                if latest is None:
                    fail(
                        "E_LINEAGE_BROKEN",
                        "no replay records are available",
                        entry="latest",
                        version=None,
                    )
                key = str(latest["export_cache_key"])
            return self.replay.replay(key)
        except BusinessError as error:
            self._record_error(error)
            raise

    def snapshot_status(self) -> dict[str, Any]:
        manifest = self.snapshots.current_manifest()
        if manifest is None:
            return {"version": None, "entry_count": 0}
        return {
            "version": int(manifest["version"]),
            "entry_count": len(manifest["entries"]),
            "config_checksum": manifest["config_checksum"],
            "manifest_checksum": manifest["manifest_checksum"],
            "generation_signature": manifest["generation_signature"],
            "entries": manifest["entries"],
        }

    def key_details(self, node: str) -> dict[str, Any]:
        if self.artifacts is None or node not in self.artifacts:
            fail(
                "E_STATE_UNAVAILABLE",
                "run an analysis before requesting cache key details",
                node=node,
            )
        artifact = self.artifacts[node]
        return {
            "node": node,
            "cache_key": artifact.cache_key,
            "node_version": artifact.node_version,
            "key_details": artifact.key_details,
        }
