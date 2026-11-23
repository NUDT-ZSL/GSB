"""Five-stage orchestrator with recovery and incremental invalidation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from .cache import (
    ContentAddressedCache,
    ResidentEmbeddingCache,
    feature_key_components,
    params_key_components,
    pcm_artifact_from_array,
    pcm_array_from_artifact,
    synth_key_components,
    timbre_key_components,
)
from .config import PipelineConfig, STAGES
from .dataset import (
    Dataset,
    read_wav,
    stable_entry_signature,
    validate_entries,
    write_wav,
)
from .dsp import (
    align_frames,
    estimate_timbre,
    extract_features,
    map_conversion_params,
    synthesize_pcm,
    validate_frame_alignment,
)
from .errors import BusinessError, fail
from .models import ManifestEntry, artifact_hash, canonical_bytes, sha256_bytes, write_json_atomic
from .snapshots import NonBlockingLock, SnapshotEntry, SnapshotState, SnapshotStore


@dataclass
class ConversionResult:
    signature: str
    utterance_ids: list[str]
    pcm_by_utterance: dict[str, np.ndarray]
    node_records: dict[str, dict[str, dict[str, Any]]]
    affected_utterance_ids: list[str]
    affected_nodes: list[str]
    recomputed_nodes: list[str]
    reused_nodes: list[str]
    snapshot_versions: dict[str, int]
    resumed_from_stage: int
    executed_stages: list[str]
    degradations: list[str] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "ok": True,
            "signature": self.signature,
            "utterance_ids": self.utterance_ids,
            "pcm_sha256": {
                utterance_id: sha256_bytes(
                    pcm.astype("<i2", copy=False).tobytes()
                )
                for utterance_id, pcm in sorted(self.pcm_by_utterance.items())
            },
            "affected_utterance_ids": self.affected_utterance_ids,
            "affected_nodes": self.affected_nodes,
            "recomputed_nodes": self.recomputed_nodes,
            "reused_nodes": self.reused_nodes,
            "snapshot_versions": self.snapshot_versions,
            "resumed_from_stage": self.resumed_from_stage,
            "executed_stages": self.executed_stages,
            "degradations": self.degradations,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


NODE_LABEL = {
    "frame_features": "features",
    "timbre_embedding": "timbre",
    "phoneme_alignment": "alignment",
    "conversion_params": "params",
    "synth_pcm": "synthesis",
}


class ConversionEngine:
    def __init__(
        self,
        *,
        dataset: Dataset,
        config: PipelineConfig | None = None,
        state_dir: str | Path = ".spkvc",
        cache_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
    ):
        self.dataset = dataset
        self.config = config or dataset.load_config()
        self.config.validate()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir = self.state_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cache = ContentAddressedCache(cache_dir or self.state_dir / "cache")
        self.snapshots = SnapshotStore(self.run_dir)
        self.output_dir = Path(output_dir or self.state_dir / "outputs")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_lock_path = self.run_dir / "stage.lock"
        self.trace_path = self.run_dir / "cache_traces.json"
        self.effective_entries_path = self.run_dir / "effective_entries.json"
        self._audio_info: dict[str, tuple[np.ndarray, str]] = {}
        self._ref_info: dict[tuple[str, int], tuple[np.ndarray, str]] = {}
        self._traces: dict[str, dict[str, Any]] = {}
        self._degradations: set[str] = set()
        self._cache_hits = 0
        self._cache_misses = 0
        self._stage_payloads: dict[str, dict[str, Any]] = {}
        self.crash_after_stage: int | None = None
        self.crash_mid_stage: int | None = None
        self.crash_after_utterances: int = 0

    def _audio(self, entry: ManifestEntry) -> tuple[np.ndarray, str]:
        if entry.utterance_id not in self._audio_info:
            pcm, raw = read_wav(
                self.dataset.utterance_audio_path(entry.utterance_id),
                self.config.sample_rate,
            )
            self._audio_info[entry.utterance_id] = (pcm, sha256_bytes(raw))
        return self._audio_info[entry.utterance_id]

    def _reference(self, ref_id: str, revision: int) -> tuple[np.ndarray, str]:
        key = (ref_id, revision)
        if key not in self._ref_info:
            pcm, raw = read_wav(
                self.dataset.ref_audio_path(ref_id, revision),
                self.config.sample_rate,
            )
            self._ref_info[key] = (pcm, sha256_bytes(raw))
        return self._ref_info[key]

    def _signature(
        self, entries: list[ManifestEntry], config: PipelineConfig
    ) -> str:
        audio_hashes = {
            entry.utterance_id: self._audio(entry)[1] for entry in entries
        }
        ref_hashes = {
            f"{entry.ref_id}:{entry.ref_revision}": self._reference(
                entry.ref_id, entry.ref_revision
            )[1]
            for entry in entries
        }
        return artifact_hash(
            {
                "manifest": stable_entry_signature(entries),
                "config": config.to_dict(),
                "audio_hashes": audio_hashes,
                "reference_hashes": ref_hashes,
            }
        )

    def _reset_metrics(self) -> None:
        self._degradations = set()
        self._cache_hits = 0
        self._cache_misses = 0

    def _note_lookup(self, lookup: Any) -> None:
        if lookup.hit:
            self._cache_hits += 1
        else:
            self._cache_misses += 1

    def _record(
        self,
        *,
        node_type: str,
        key: str,
        components: dict[str, Any],
        artifact_value: Any,
        artifact_hash_value: str,
        hit: bool,
        cache_path: str | None,
        reused_from_snapshot: bool = False,
    ) -> dict[str, Any]:
        return {
            "node": NODE_LABEL[node_type],
            "node_type": node_type,
            "key": key,
            "key_components": components,
            "artifact_hash": artifact_hash_value,
            "hit": bool(hit),
            "reused_from_snapshot": bool(reused_from_snapshot),
            "cache_path": cache_path,
            "artifact_sha256": artifact_hash(artifact_value),
        }

    def _commit_stage(
        self,
        *,
        stage_index: int,
        entries: list[ManifestEntry],
        artifacts: dict[str, Any],
        node_records: dict[str, dict[str, Any]],
        signature: str,
    ) -> SnapshotEntry:
        completed = sorted(artifacts)
        expected = sorted(entry.utterance_id for entry in entries)
        if completed != expected:
            fail(
                "E_STAGE_INCOMPLETE",
                "阶段未覆盖全部 utterance，禁止提交",
                field="completed_utterance_ids",
                details={
                    "stage": STAGES[stage_index],
                    "missing": sorted(set(expected) - set(completed)),
                    "unexpected": sorted(set(completed) - set(expected)),
                },
            )
        payload = {
            "stage": STAGES[stage_index],
            "stage_index": stage_index,
            "completed_utterance_ids": completed,
            "artifacts": {key: artifacts[key] for key in completed},
            "node_records": {key: node_records[key] for key in completed},
        }
        entry = self.snapshots.commit_stage(
            stage_index=stage_index,
            completed_utterance_ids=completed,
            payload=payload,
            signature=signature,
        )
        self._stage_payloads[STAGES[stage_index]] = payload
        for utterance_id, record in node_records.items():
            self._traces.setdefault(utterance_id, {})[record["node"]] = record
        self._persist_traces()
        self._persist_effective_config(signature)
        return entry

    def _persist_traces(self) -> None:
        write_json_atomic(
            self.trace_path,
            {
                "schema_version": 1,
                "utterances": {
                    utterance_id: {
                        node: record for node, record in sorted(nodes.items())
                    }
                    for utterance_id, nodes in sorted(self._traces.items())
                },
            },
        )

    def _persist_effective_config(self, signature: str) -> None:
        write_json_atomic(
            self.run_dir / "effective_config.json",
            {
                "schema_version": 1,
                "signature": signature,
                "config": self.config.to_dict(),
            },
        )

    def _persist_effective_entries(
        self, entries: list[ManifestEntry], signature: str
    ) -> None:
        write_json_atomic(
            self.effective_entries_path,
            {
                "schema_version": 1,
                "signature": signature,
                "entries": [entry.to_dict() for entry in entries],
            },
        )

    def _effective_entries_if_matching(
        self, state: SnapshotState, current_signature: str
    ) -> tuple[list[ManifestEntry], str] | None:
        if not state.entries or not self.effective_entries_path.is_file():
            return None
        if state.signature == current_signature:
            return None
        try:
            value = json.loads(
                self.effective_entries_path.read_text(encoding="utf-8")
            )
            entries = [
                ManifestEntry.from_dict(item) for item in value["entries"]
            ]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        entries = sorted(entries, key=lambda item: item.utterance_id)
        try:
            candidate_signature = self._signature(entries, self.config)
        except BusinessError:
            return None
        if candidate_signature != state.signature:
            return None
        return entries, candidate_signature

    def _load_snapshot_state(
        self, signature: str
    ) -> tuple[SnapshotState, dict[str, dict[str, Any]]]:
        state = self.snapshots.load()
        if state.entries and state.signature != signature:
            fail(
                "E_STATE_SIGNATURE_MISMATCH",
                "已提交快照与当前输入或配置不一致，请显式执行增量或清理状态",
                field="signature",
                details={
                    "committed": state.signature,
                    "requested": signature,
                },
            )
        payloads: dict[str, dict[str, Any]] = {}
        for entry in state.entries:
            payload_path = self.snapshots.payload_path(entry)
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "无法读取已提交阶段 payload",
                    field="payload_file",
                    details={
                        "stage": entry.stage,
                        "path": str(payload_path),
                        "reason": str(exc),
                    },
                )
            payloads[entry.stage] = payload
            for utterance_id, record in payload.get("node_records", {}).items():
                record = dict(record)
                record["reused_from_snapshot"] = True
                self._traces.setdefault(utterance_id, {})[record["node"]] = record
        return state, payloads

    def _maybe_crash_mid(self, stage_index: int, processed: int) -> None:
        if (
            self.crash_mid_stage == stage_index
            and self.crash_after_utterances > 0
            and processed >= self.crash_after_utterances
        ):
            os._exit(87)

    def _feature_stage(
        self, entries: list[ManifestEntry]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        artifacts: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        for processed, entry in enumerate(entries, start=1):
            pcm, raw_hash = self._audio(entry)
            components = feature_key_components(entry, raw_hash, self.config)
            lookup = self.cache.get_or_compute(
                "frame_features",
                components,
                lambda entry=entry, pcm=pcm, raw_hash=raw_hash: extract_features(
                    entry=entry,
                    pcm=pcm,
                    raw_pcm_hash=raw_hash,
                    config=self.config,
                ),
            )
            self._note_lookup(lookup)
            artifacts[entry.utterance_id] = lookup.artifact
            records[entry.utterance_id] = self._record(
                node_type="frame_features",
                key=lookup.key,
                components=components,
                artifact_value=lookup.artifact,
                artifact_hash_value=lookup.artifact_hash,
                hit=lookup.hit,
                cache_path=lookup.path,
            )
            self._maybe_crash_mid(0, processed)
        return artifacts, records

    def _timbre_artifact(
        self,
        entry: ManifestEntry,
        *,
        embedding_cache: ResidentEmbeddingCache,
    ) -> Any:
        ref_pcm, ref_hash = self._reference(entry.ref_id, entry.ref_revision)
        components = timbre_key_components(
            entry,
            ref_audio_chunk_sha256=ref_hash,
            config=self.config,
        )
        cached = embedding_cache.get(entry.ref_id)
        if cached is not None and cached[0] == components:
            return replace(cached[1], hit=True)
        lookup = self.cache.get_or_compute(
            "timbre_embedding",
            components,
            lambda: estimate_timbre(
                ref_id=entry.ref_id,
                ref_speaker_id=self.dataset.ref_speaker_id(entry.ref_id),
                ref_revision=entry.ref_revision,
                ref_pcm=ref_pcm,
                ref_pcm_hash=ref_hash,
                config=self.config,
                precision_profile=entry.precision_profile,
            ),
        )
        embedding_cache.put(entry.ref_id, (components, lookup))
        return lookup

    def _timbre_stage(
        self, entries: list[ManifestEntry]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        artifacts: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        embedding_cache = ResidentEmbeddingCache(
            self.config.resource_limits.max_resident_embeddings
        )
        for processed, entry in enumerate(entries, start=1):
            lookup = self._timbre_artifact(entry, embedding_cache=embedding_cache)
            self._note_lookup(lookup)
            artifacts[entry.utterance_id] = lookup.artifact
            records[entry.utterance_id] = self._record(
                node_type="timbre_embedding",
                key=lookup.key,
                components=lookup.components,
                artifact_value=lookup.artifact,
                artifact_hash_value=lookup.artifact_hash,
                hit=lookup.hit,
                cache_path=lookup.path,
            )
            self._maybe_crash_mid(1, processed)
        if embedding_cache.evictions:
            self._degradations.add("embedding_cache_lru")
        return artifacts, records

    def _alignment_artifact(
        self,
        features: dict[str, Any],
        timbre: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        components = {
            "node_type": "phoneme_alignment",
            "schema_version": 1,
            "feature_artifact_hash": artifact_hash(features),
            "timbre_artifact_hash": artifact_hash(timbre),
            "alignment_version": self.config.alignment_version,
            "tie_break": ["score_desc", "speaker_id_asc", "phoneme_id_asc"],
        }
        alignment = align_frames(
            features=features, timbre=timbre, config=self.config
        )
        return alignment, components, artifact_hash(alignment)

    def _alignment_stage(
        self,
        entries: list[ManifestEntry],
        *,
        feature_artifacts: dict[str, Any],
        timbre_artifacts: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        artifacts: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        for processed, entry in enumerate(entries, start=1):
            features = feature_artifacts[entry.utterance_id]
            timbre = timbre_artifacts[entry.utterance_id]
            alignment, components, alignment_hash = self._alignment_artifact(
                features, timbre
            )
            key = sha256_bytes(canonical_bytes(components))
            artifacts[entry.utterance_id] = alignment
            records[entry.utterance_id] = self._record(
                node_type="phoneme_alignment",
                key=key,
                components=components,
                artifact_value=alignment,
                artifact_hash_value=alignment_hash,
                hit=False,
                cache_path=None,
            )
            self._maybe_crash_mid(2, processed)
        return artifacts, records

    def _params_artifact(
        self,
        entry: ManifestEntry,
        *,
        features: dict[str, Any],
        timbre: dict[str, Any],
        alignment: dict[str, Any],
    ) -> Any:
        pcm, raw_hash = self._audio(entry)
        del pcm
        _, ref_hash = self._reference(entry.ref_id, entry.ref_revision)
        upstream = {
            "frame_features": artifact_hash(features),
            "timbre_embedding": artifact_hash(timbre),
            "phoneme_alignment": artifact_hash(alignment),
        }
        components = params_key_components(
            entry,
            audio_chunk_sha256=raw_hash,
            ref_audio_chunk_sha256=ref_hash,
            upstream_hashes=upstream,
            config=self.config,
        )
        decimals = self._precision_decimals(entry.precision_profile)
        return self.cache.get_or_compute(
            "conversion_params",
            components,
            lambda: map_conversion_params(
                features=features,
                timbre=timbre,
                alignment=alignment,
                config=self.config,
                decimals=decimals,
            ),
        )

    @staticmethod
    def _precision_decimals(profile: str) -> int:
        from .config import decimals_for

        return decimals_for(profile)

    def _params_stage(
        self,
        entries: list[ManifestEntry],
        *,
        feature_artifacts: dict[str, Any],
        timbre_artifacts: dict[str, Any],
        alignment_artifacts: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        artifacts: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        for processed, entry in enumerate(entries, start=1):
            lookup = self._params_artifact(
                entry,
                features=feature_artifacts[entry.utterance_id],
                timbre=timbre_artifacts[entry.utterance_id],
                alignment=alignment_artifacts[entry.utterance_id],
            )
            self._note_lookup(lookup)
            artifacts[entry.utterance_id] = lookup.artifact
            records[entry.utterance_id] = self._record(
                node_type="conversion_params",
                key=lookup.key,
                components=lookup.components,
                artifact_value=lookup.artifact,
                artifact_hash_value=lookup.artifact_hash,
                hit=lookup.hit,
                cache_path=lookup.path,
            )
            self._maybe_crash_mid(3, processed)
        return artifacts, records

    def _synth_artifact(
        self,
        entry: ManifestEntry,
        *,
        params: dict[str, Any],
    ) -> tuple[Any, bool]:
        pcm, raw_hash = self._audio(entry)
        del pcm
        _, ref_hash = self._reference(entry.ref_id, entry.ref_revision)
        params_hash = artifact_hash(params)
        components = synth_key_components(
            entry,
            audio_chunk_sha256=raw_hash,
            ref_audio_chunk_sha256=ref_hash,
            params_artifact_hash=params_hash,
            config=self.config,
        )
        existing = self.cache.get_record("synth_pcm", components)
        if existing is not None:
            cached_pcm = pcm_array_from_artifact(existing.artifact)
            self._validate_truth(entry, cached_pcm)
            return existing, True
        pcm_out = synthesize_pcm(params, self.config)
        self._validate_truth(entry, pcm_out)
        artifact = pcm_artifact_from_array(
            pcm_out,
            utterance_id=entry.utterance_id,
            params_artifact_hash=params_hash,
            sample_rate=self.config.sample_rate,
            frame_count=int(params["frame_count"]),
        )
        return self.cache.put("synth_pcm", components, artifact), False

    def _validate_truth(self, entry: ManifestEntry, pcm: np.ndarray) -> None:
        truth = self.dataset.read_truth(entry.utterance_id)
        truth_count = len(truth)
        feature_count: int | None = None
        for stage_index in range(0, 5):
            stage = STAGES[stage_index]
            payload = self._stage_payloads.get(stage)
            if payload and entry.utterance_id in payload.get("artifacts", {}):
                artifact = payload["artifacts"][entry.utterance_id]
                if stage == "features":
                    feature_count = int(artifact["frame_count"])
                    break
        if feature_count is None:
            feature_count = int(round(len(pcm) / self.config.hop_samples))
        validate_frame_alignment(
            entry=entry,
            pcm=pcm,
            frame_count=feature_count,
            truth_count=int(truth_count),
            config=self.config,
        )

    def _synth_stage(
        self,
        entries: list[ManifestEntry],
        *,
        params_artifacts: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        artifacts: dict[str, Any] = {}
        records: dict[str, dict[str, Any]] = {}
        for processed, entry in enumerate(entries, start=1):
            lookup, cached = self._synth_artifact(
                entry, params=params_artifacts[entry.utterance_id]
            )
            if cached:
                self._cache_hits += 1
            else:
                self._cache_misses += 1
            artifacts[entry.utterance_id] = lookup.artifact
            records[entry.utterance_id] = self._record(
                node_type="synth_pcm",
                key=lookup.key,
                components=lookup.components,
                artifact_value=lookup.artifact,
                artifact_hash_value=lookup.artifact_hash,
                hit=lookup.hit,
                cache_path=lookup.path,
            )
            self._maybe_crash_mid(4, processed)
        return artifacts, records

    def _stage_outputs(self, stage: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        payload = self._stage_payloads[stage]
        return payload["artifacts"], payload["node_records"]

    def _write_outputs(self, entries: list[ManifestEntry]) -> dict[str, np.ndarray]:
        synth_artifacts, _ = self._stage_outputs("synthesis")
        output: dict[str, np.ndarray] = {}
        for entry in entries:
            artifact = synth_artifacts[entry.utterance_id]
            pcm = pcm_array_from_artifact(artifact)
            self._validate_truth(entry, pcm)
            write_wav(
                self.output_dir / f"{entry.utterance_id}.wav",
                pcm,
                self.config.sample_rate,
            )
            output[entry.utterance_id] = pcm
        return output

    def _outcome_lists(
        self,
        entries: list[ManifestEntry],
        affected_nodes: list[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        recomputed: list[str] = []
        reused: list[str] = []
        all_nodes: list[str] = []
        for entry in entries:
            for node in ("features", "timbre", "alignment", "params", "synthesis"):
                node_id = f"{node}:{entry.utterance_id}"
                all_nodes.append(node_id)
                record = self._traces.get(entry.utterance_id, {}).get(node)
                if record is None:
                    continue
                if record.get("hit") or record.get("reused_from_snapshot"):
                    reused.append(node_id)
                else:
                    recomputed.append(node_id)
        if affected_nodes is not None:
            ordered = set(affected_nodes)
            recomputed = [item for item in recomputed if item in ordered or item not in all_nodes]
            reused = [item for item in reused if item not in ordered]
        return sorted(set(recomputed)), sorted(set(reused))

    def run(
        self,
        entries: list[ManifestEntry] | None = None,
        *,
        crash_after_stage: int | None = None,
        crash_mid_stage: int | None = None,
        crash_after_utterances: int = 0,
    ) -> ConversionResult:
        with NonBlockingLock(self.run_lock_path, name="stage"):
            self._reset_metrics()
            if self.config.parallel_nodes > self.config.resource_limits.max_parallel_nodes:
                self._degradations.add("parallel_nodes_capped")
            manifest = entries if entries is not None else self.dataset.load_entries()
            validate_entries(manifest, dataset=self.dataset, config=self.config)
            manifest = sorted(manifest, key=lambda item: item.utterance_id)
            signature = self._signature(manifest, self.config)
            state = self.snapshots.load()
            if entries is None:
                effective = self._effective_entries_if_matching(
                    state, signature
                )
                if effective is not None:
                    manifest, signature = effective
            state, payloads = self._load_snapshot_state(signature)
            self._stage_payloads = payloads
            self.crash_after_stage = crash_after_stage
            self.crash_mid_stage = crash_mid_stage
            self.crash_after_utterances = crash_after_utterances
            executed: list[str] = []
            snapshot_versions = {
                entry.stage: entry.version for entry in state.entries
            }
            for stage_index in range(len(state.entries), len(STAGES)):
                stage = STAGES[stage_index]
                if stage_index == 0:
                    artifacts, records = self._feature_stage(manifest)
                elif stage_index == 1:
                    artifacts, records = self._timbre_stage(manifest)
                elif stage_index == 2:
                    feature_artifacts, _ = self._stage_outputs("features")
                    timbre_artifacts, _ = self._stage_outputs("timbre")
                    artifacts, records = self._alignment_stage(
                        manifest,
                        feature_artifacts=feature_artifacts,
                        timbre_artifacts=timbre_artifacts,
                    )
                elif stage_index == 3:
                    feature_artifacts, _ = self._stage_outputs("features")
                    timbre_artifacts, _ = self._stage_outputs("timbre")
                    alignment_artifacts, _ = self._stage_outputs("alignment")
                    artifacts, records = self._params_stage(
                        manifest,
                        feature_artifacts=feature_artifacts,
                        timbre_artifacts=timbre_artifacts,
                        alignment_artifacts=alignment_artifacts,
                    )
                else:
                    params_artifacts, _ = self._stage_outputs("params")
                    artifacts, records = self._synth_stage(
                        manifest, params_artifacts=params_artifacts
                    )
                snapshot_entry = self._commit_stage(
                    stage_index=stage_index,
                    entries=manifest,
                    artifacts=artifacts,
                    node_records=records,
                    signature=signature,
                )
                snapshot_versions[stage] = snapshot_entry.version
                executed.append(stage)
                if crash_after_stage == stage_index:
                    os._exit(86)
            if len(self._stage_payloads) != len(STAGES):
                fail(
                    "E_STAGE_INCOMPLETE",
                    "五阶段未全部完成",
                    field="stage_index",
                    details={"completed": sorted(self._stage_payloads)},
                )
            output = self._write_outputs(manifest)
            self._persist_effective_entries(manifest, signature)
            affected = [
                f"{node}:{entry.utterance_id}"
                for entry in manifest
                for node in ("features", "timbre", "alignment", "params", "synthesis")
            ]
            recomputed, reused = self._outcome_lists(manifest)
            if self._cache_hits == 0 and self._cache_misses == 0:
                reused = sorted(affected)
                recomputed = []
            return ConversionResult(
                signature=signature,
                utterance_ids=[entry.utterance_id for entry in manifest],
                pcm_by_utterance=output,
                node_records={
                    utterance_id: dict(nodes)
                    for utterance_id, nodes in self._traces.items()
                },
                affected_utterance_ids=[entry.utterance_id for entry in manifest],
                affected_nodes=sorted(affected),
                recomputed_nodes=recomputed,
                reused_nodes=reused,
                snapshot_versions=snapshot_versions,
                resumed_from_stage=len(state.entries),
                executed_stages=executed,
                degradations=sorted(self._degradations),
                cache_hits=self._cache_hits,
                cache_misses=self._cache_misses,
            )

    def incremental(
        self,
        *,
        ref_revisions: dict[str, int] | None = None,
        new_config: PipelineConfig | None = None,
        crash_mid_stage: int | None = None,
        crash_after_utterances: int = 0,
    ) -> ConversionResult:
        with NonBlockingLock(self.run_lock_path, name="stage"):
            self._reset_metrics()
            self.crash_mid_stage = crash_mid_stage
            self.crash_after_utterances = crash_after_utterances
            old_entries = sorted(
                self.dataset.load_entries(), key=lambda item: item.utterance_id
            )
            validate_entries(old_entries, dataset=self.dataset, config=self.config)
            state = self.snapshots.load()
            if len(state.entries) != len(STAGES):
                fail(
                    "E_INCREMENT_REQUIRES_BASELINE",
                    "增量失效要求完整基线五阶段快照",
                    field="snapshot_state",
                    details={"committed_stages": len(state.entries)},
                )
            self._stage_payloads = {
                entry.stage: json.loads(
                    self.snapshots.payload_path(entry).read_text(encoding="utf-8")
                )
                for entry in state.entries
            }
            old_config = self.config
            config = new_config or self.config
            config.validate()
            revisions = dict(ref_revisions or {})
            new_entries = [
                entry.with_ref_revision(revisions.get(entry.ref_id, entry.ref_revision))
                for entry in old_entries
            ]
            validate_entries(new_entries, dataset=self.dataset, config=config)
            allowed_changes = {
                "mapping_version",
                "mapping_params",
                "parallel_nodes",
                "resource_limits",
            }
            old_config_dict = old_config.to_dict()
            new_config_dict = config.to_dict()
            non_increment_changes = [
                key
                for key in old_config_dict
                if key not in allowed_changes
                and old_config_dict[key] != new_config_dict.get(key)
            ]
            if non_increment_changes:
                fail(
                    "E_UNSUPPORTED_INCREMENT",
                    "增量仅支持参考片段或映射参数变化",
                    field=non_increment_changes[0],
                    details={"changed_fields": sorted(non_increment_changes)},
                )
            old_timbre_artifacts, _ = self._stage_outputs_snapshot("timbre")
            changed_ref_ids: set[str] = set()
            for old_entry, new_entry in zip(old_entries, new_entries):
                old_timbre = old_timbre_artifacts[old_entry.utterance_id]
                new_revision = new_entry.ref_revision
                _, new_ref_hash = self._reference(new_entry.ref_id, new_revision)
                if (
                    new_revision != old_entry.ref_revision
                    or new_ref_hash != old_timbre.get("ref_audio_sha256")
                ):
                    changed_ref_ids.add(new_entry.ref_id)
            ref_affected = {
                entry.utterance_id
                for entry in new_entries
                if entry.ref_id in changed_ref_ids
            }
            mapping_changed = (
                old_config.mapping_config_hash() != config.mapping_config_hash()
            )
            mapping_affected = (
                {entry.utterance_id for entry in new_entries}
                if mapping_changed
                else set()
            )
            affected_utterances = ref_affected | mapping_affected
            affected_count = len(affected_utterances)
            if affected_count > config.resource_limits.max_recompute_utterances:
                fail(
                    "E_RESOURCE_LIMIT",
                    "单次增量重算 utterance 数超过上限",
                    field="max_recompute_utterances",
                    details={
                        "affected_utterance_count": affected_count,
                        "max_recompute_utterances": (
                            config.resource_limits.max_recompute_utterances
                        ),
                    },
                )
            if config.parallel_nodes > config.resource_limits.max_parallel_nodes:
                self._degradations.add("parallel_nodes_capped")
            self.config = config
            new_payloads = {
                stage: {
                    "stage": stage,
                    "stage_index": STAGES.index(stage),
                    "completed_utterance_ids": [
                        entry.utterance_id for entry in new_entries
                    ],
                    "artifacts": {},
                    "node_records": {},
                }
                for stage in STAGES
            }
            recomputed_nodes: set[str] = set()
            reused_nodes: set[str] = set()
            affected_nodes: list[str] = []
            for utterance_id in sorted(affected_utterances):
                if utterance_id in ref_affected:
                    affected_nodes.extend(
                        [
                            f"timbre:{utterance_id}",
                            f"alignment:{utterance_id}",
                        ]
                    )
                affected_nodes.extend(
                    [
                        f"params:{utterance_id}",
                        f"synthesis:{utterance_id}",
                    ]
                )
            affected_nodes = sorted(set(affected_nodes))

            old_features, old_feature_records = self._snapshot_stage("features")
            for entry in new_entries:
                utterance_id = entry.utterance_id
                new_payloads["features"]["artifacts"][utterance_id] = old_features[
                    utterance_id
                ]
                record = dict(old_feature_records[utterance_id])
                record["reused_from_snapshot"] = True
                new_payloads["features"]["node_records"][utterance_id] = record
                reused_nodes.add(f"features:{utterance_id}")

            old_timbre, old_timbre_records = self._snapshot_stage("timbre")
            embedding_cache = ResidentEmbeddingCache(
                config.resource_limits.max_resident_embeddings
            )
            for processed, entry in enumerate(new_entries, start=1):
                utterance_id = entry.utterance_id
                node_id = f"timbre:{utterance_id}"
                if utterance_id in ref_affected:
                    lookup = self._timbre_artifact(
                        entry, embedding_cache=embedding_cache
                    )
                    self._note_lookup(lookup)
                    new_payloads["timbre"]["artifacts"][utterance_id] = lookup.artifact
                    record = self._record(
                        node_type="timbre_embedding",
                        key=lookup.key,
                        components=lookup.components,
                        artifact_value=lookup.artifact,
                        artifact_hash_value=lookup.artifact_hash,
                        hit=lookup.hit,
                        cache_path=lookup.path,
                    )
                    (reused_nodes if lookup.hit else recomputed_nodes).add(node_id)
                else:
                    new_payloads["timbre"]["artifacts"][utterance_id] = old_timbre[
                        utterance_id
                    ]
                    record = dict(old_timbre_records[utterance_id])
                    record["reused_from_snapshot"] = True
                    reused_nodes.add(node_id)
                new_payloads["timbre"]["node_records"][utterance_id] = record
                self._maybe_crash_mid(1, processed)
            if embedding_cache.evictions:
                self._degradations.add("embedding_cache_lru")

            old_alignment, old_alignment_records = self._snapshot_stage("alignment")
            for processed, entry in enumerate(new_entries, start=1):
                utterance_id = entry.utterance_id
                node_id = f"alignment:{utterance_id}"
                if utterance_id in ref_affected:
                    features = new_payloads["features"]["artifacts"][utterance_id]
                    timbre = new_payloads["timbre"]["artifacts"][utterance_id]
                    alignment, components, alignment_hash = self._alignment_artifact(
                        features, timbre
                    )
                    record = self._record(
                        node_type="phoneme_alignment",
                        key=sha256_bytes(canonical_bytes(components)),
                        components=components,
                        artifact_value=alignment,
                        artifact_hash_value=alignment_hash,
                        hit=False,
                        cache_path=None,
                    )
                    new_payloads["alignment"]["artifacts"][utterance_id] = alignment
                    recomputed_nodes.add(node_id)
                else:
                    new_payloads["alignment"]["artifacts"][utterance_id] = old_alignment[
                        utterance_id
                    ]
                    record = dict(old_alignment_records[utterance_id])
                    record["reused_from_snapshot"] = True
                    reused_nodes.add(node_id)
                new_payloads["alignment"]["node_records"][utterance_id] = record
                self._maybe_crash_mid(2, processed)

            old_params, old_params_records = self._snapshot_stage("params")
            for processed, entry in enumerate(new_entries, start=1):
                utterance_id = entry.utterance_id
                node_id = f"params:{utterance_id}"
                if utterance_id in affected_utterances:
                    lookup = self._params_artifact(
                        entry,
                        features=new_payloads["features"]["artifacts"][utterance_id],
                        timbre=new_payloads["timbre"]["artifacts"][utterance_id],
                        alignment=new_payloads["alignment"]["artifacts"][utterance_id],
                    )
                    self._note_lookup(lookup)
                    new_payloads["params"]["artifacts"][utterance_id] = lookup.artifact
                    record = self._record(
                        node_type="conversion_params",
                        key=lookup.key,
                        components=lookup.components,
                        artifact_value=lookup.artifact,
                        artifact_hash_value=lookup.artifact_hash,
                        hit=lookup.hit,
                        cache_path=lookup.path,
                    )
                    (reused_nodes if lookup.hit else recomputed_nodes).add(node_id)
                else:
                    new_payloads["params"]["artifacts"][utterance_id] = old_params[
                        utterance_id
                    ]
                    record = dict(old_params_records[utterance_id])
                    record["reused_from_snapshot"] = True
                    reused_nodes.add(node_id)
                new_payloads["params"]["node_records"][utterance_id] = record
                self._maybe_crash_mid(3, processed)

            old_synth, old_synth_records = self._snapshot_stage("synthesis")
            for processed, entry in enumerate(new_entries, start=1):
                utterance_id = entry.utterance_id
                node_id = f"synthesis:{utterance_id}"
                if utterance_id in affected_utterances:
                    lookup, cached = self._synth_artifact(
                        entry,
                        params=new_payloads["params"]["artifacts"][utterance_id],
                    )
                    if cached:
                        self._cache_hits += 1
                    else:
                        self._cache_misses += 1
                    new_payloads["synthesis"]["artifacts"][utterance_id] = lookup.artifact
                    record = self._record(
                        node_type="synth_pcm",
                        key=lookup.key,
                        components=lookup.components,
                        artifact_value=lookup.artifact,
                        artifact_hash_value=lookup.artifact_hash,
                        hit=lookup.hit,
                        cache_path=lookup.path,
                    )
                    (reused_nodes if lookup.hit else recomputed_nodes).add(node_id)
                else:
                    new_payloads["synthesis"]["artifacts"][utterance_id] = old_synth[
                        utterance_id
                    ]
                    record = dict(old_synth_records[utterance_id])
                    record["reused_from_snapshot"] = True
                    reused_nodes.add(node_id)
                new_payloads["synthesis"]["node_records"][utterance_id] = record
                self._maybe_crash_mid(4, processed)

            signature = self._signature(new_entries, config)
            self._persist_effective_entries(new_entries, signature)
            self._persist_effective_config(signature)
            stage_payloads = [
                (
                    index,
                    [entry.utterance_id for entry in new_entries],
                    new_payloads[stage],
                )
                for index, stage in enumerate(STAGES)
            ]
            entries_committed = self.snapshots.replace_all(
                stages=stage_payloads, signature=signature
            )
            self._stage_payloads = new_payloads
            self._traces = {}
            for payload in new_payloads.values():
                for utterance_id, record in payload["node_records"].items():
                    self._traces.setdefault(utterance_id, {})[
                        record["node"]
                    ] = record
            self._persist_traces()
            self._persist_effective_config(signature)
            output = self._write_outputs(new_entries)
            return ConversionResult(
                signature=signature,
                utterance_ids=[entry.utterance_id for entry in new_entries],
                pcm_by_utterance=output,
                node_records={
                    utterance_id: dict(nodes)
                    for utterance_id, nodes in self._traces.items()
                },
                affected_utterance_ids=sorted(affected_utterances),
                affected_nodes=affected_nodes,
                recomputed_nodes=sorted(recomputed_nodes),
                reused_nodes=sorted(reused_nodes),
                snapshot_versions={
                    entry.stage: entry.version for entry in entries_committed
                },
                resumed_from_stage=5,
                executed_stages=sorted(
                    {
                        node_id.split(":", 1)[0]
                        for node_id in recomputed_nodes
                    }
                ),
                degradations=sorted(self._degradations),
                cache_hits=self._cache_hits,
                cache_misses=self._cache_misses,
            )

    def _stage_outputs_snapshot(
        self, stage: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        return self._snapshot_stage(stage)

    def _snapshot_stage(
        self, stage: str
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if stage not in self._stage_payloads:
            fail(
                "E_STAGE_INCOMPLETE",
                "缺少上游阶段快照",
                field="stage",
                details={"stage": stage},
            )
        payload = self._stage_payloads[stage]
        return payload["artifacts"], payload["node_records"]

    def cache_report(self, utterance_id: str) -> dict[str, Any]:
        traces = self._traces
        if not traces and self.trace_path.is_file():
            try:
                value = json.loads(self.trace_path.read_text(encoding="utf-8"))
                traces = value.get("utterances", {})
            except (OSError, json.JSONDecodeError) as exc:
                fail(
                    "E_CACHE_TRACE_CORRUPT",
                    "缓存追溯文件损坏",
                    field="cache_trace",
                    details={"path": str(self.trace_path), "reason": str(exc)},
                )
        nodes = traces.get(utterance_id)
        if not nodes:
            fail(
                "E_UTTERANCE_NOT_FOUND",
                "没有该 utterance 的缓存追溯记录",
                field="utterance_id",
                details={"utterance_id": utterance_id},
            )
        cache_node_names = {"features", "timbre", "params", "synthesis"}
        cached_nodes = {
            name: dict(record)
            for name, record in sorted(nodes.items())
            if name in cache_node_names
        }
        derived_nodes = {
            name: dict(record)
            for name, record in sorted(nodes.items())
            if name not in cache_node_names
        }
        return {
            "ok": True,
            "utterance_id": utterance_id,
            "nodes": cached_nodes,
            "derived_nodes": derived_nodes,
            "reuse_check": {
                "keys_present": all(
                    bool(record.get("key")) for record in cached_nodes.values()
                ),
                "artifact_hashes_present": all(
                    bool(record.get("artifact_hash"))
                    for record in cached_nodes.values()
                ),
                "node_count": len(cached_nodes),
            },
        }

    def self_check(self, result: ConversionResult | None = None) -> dict[str, Any]:
        from .selfcheck import run_self_check

        return run_self_check(self, result)

    def self_check_snapshots(self) -> dict[str, Any]:
        from .selfcheck import run_self_check

        state = self.snapshots.load()
        if len(state.entries) != len(STAGES):
            fail(
                "E_SELF_CHECK_FAILED",
                "自检要求完整五阶段快照",
                field="snapshot_state",
                details={"committed_stages": len(state.entries)},
            )
        self._stage_payloads = {
            entry.stage: json.loads(
                self.snapshots.payload_path(entry).read_text(encoding="utf-8")
            )
            for entry in state.entries
        }
        utterance_ids = sorted(state.entries[-1].completed_utterance_ids)
        self._traces = {}
        for payload in self._stage_payloads.values():
            for utterance_id, record in payload["node_records"].items():
                updated = dict(record)
                updated["reused_from_snapshot"] = True
                self._traces.setdefault(utterance_id, {})[
                    updated["node"]
                ] = updated
        synth_artifacts, _ = self._snapshot_stage("synthesis")
        pcm_by_utterance = {
            utterance_id: pcm_array_from_artifact(synth_artifacts[utterance_id])
            for utterance_id in utterance_ids
        }
        nodes = [
            f"{node}:{utterance_id}"
            for utterance_id in utterance_ids
            for node in ("features", "timbre", "alignment", "params", "synthesis")
        ]
        result = ConversionResult(
            signature=state.signature,
            utterance_ids=utterance_ids,
            pcm_by_utterance=pcm_by_utterance,
            node_records={
                utterance_id: dict(nodes_for_utterance)
                for utterance_id, nodes_for_utterance in self._traces.items()
            },
            affected_utterance_ids=utterance_ids,
            affected_nodes=sorted(nodes),
            recomputed_nodes=[],
            reused_nodes=sorted(nodes),
            snapshot_versions={
                entry.stage: entry.version for entry in state.entries
            },
            resumed_from_stage=5,
            executed_stages=[],
            cache_hits=0,
            cache_misses=0,
        )
        return run_self_check(self, result)

    def evaluate(
        self, utterance_ids: list[str] | None = None
    ) -> dict[str, Any]:
        from .evaluation import evaluate_snapshots

        if len(self._stage_payloads) != len(STAGES):
            state = self.snapshots.load()
            if len(state.entries) != len(STAGES):
                fail(
                    "E_STAGE_INCOMPLETE",
                    "评估要求完整五阶段快照",
                    field="snapshot_state",
                    details={"committed_stages": len(state.entries)},
                )
            self._stage_payloads = {
                entry.stage: json.loads(
                    self.snapshots.payload_path(entry).read_text(
                        encoding="utf-8"
                    )
                )
                for entry in state.entries
            }
        return evaluate_snapshots(self, utterance_ids)

