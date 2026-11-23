"""Five-node computation graph, incremental execution, and lineage replay."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

from .cache import ContentAddressedCache
from .canonical import (
    atomic_write_json,
    b64_bytes,
    b64_f64,
    bytes_b64,
    digest,
    float_text,
    payload_checksum,
    read_json,
)
from .errors import (
    BusinessError,
    E_CACHE_CORRUPT,
    E_CACHE_MISS,
    E_HIT_NOT_FOUND,
    E_INPUT_INVALID,
    E_LINEAGE_BROKEN,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_STATE_INVALID,
)
from .models import (
    ALIGN_VERSION,
    DECODER_VERSION,
    RANK_VERSION,
    TEXT_EMBEDDING_DIM,
    TEXT_VERSION,
    VISUAL_VERSION,
    FeatureConfig,
    RetrievalConfig,
    SampleRecord,
    Vocabulary,
    natural_key,
)
from .retrieval import SIMILARITY_DECLARATION, align_from_artifacts, build_ranking, cosine_score
from .snapshots import STAGE_ORDER, StageSnapshotStore, SubmissionLock, VersionedRegistry
from .text import embed_text, tokenize_text, vector_from_text_artifact
from .vision import (
    decode_frame,
    extract_visual_features,
    frame_hash,
    validate_pixel_budget,
)


class ForcedTermination(Exception):
    """Test/CLI fault injection after a stage has been committed."""

    def __init__(self, stage: str):
        super().__init__(f"forced termination after {stage}")
        self.stage = stage


@dataclass
class RunOutputs:
    run_id: str
    recovery: Dict[str, Any]
    affected: Dict[str, Any]
    recomputed_nodes: List[Dict[str, Any]]
    reused_nodes: List[Dict[str, Any]]
    ranking: Dict[str, Any]
    retrieval_snapshot: Dict[str, Any]
    stages: Dict[str, Any]
    cache_key_preview: Dict[str, Any]
    degradation: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "recovery": self.recovery,
            "affected": self.affected,
            "recomputed_nodes": self.recomputed_nodes,
            "reused_nodes": self.reused_nodes,
            "ranking": self.ranking,
            "retrieval_snapshot": {
                "version": self.retrieval_snapshot.get("version"),
                "checksum": self.retrieval_snapshot.get("checksum"),
                "payload_checksum": self.retrieval_snapshot.get("payload_checksum"),
                "revision": self.retrieval_snapshot.get("revision"),
            },
            "stages": self.stages,
            "cache_key_preview": self.cache_key_preview,
            "degradation": self.degradation,
        }


class ComputationEngine:
    NODES = ("decode", "visual", "text", "align", "ranking")
    LOCAL_NODES = ("decode", "visual", "text", "align")

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.workspace / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.state_dir / "manifest.json"
        self.hit_index_path = self.state_dir / "hit_index.json"
        self.cache = ContentAddressedCache(self.workspace / "cache")
        self.registry = VersionedRegistry(self.workspace / "snapshots" / "registry")
        self.stages = StageSnapshotStore(self.workspace / "snapshots" / "stages")
        self.lock = SubmissionLock(self.workspace / "snapshots" / "submission.lock")

    # ------------------------------------------------------------------
    # Public graph and state views
    # ------------------------------------------------------------------
    @classmethod
    def topology(cls) -> Dict[str, Any]:
        nodes = [
            {
                "id": "decode",
                "label": "画面解码与分块",
                "scope": "pair",
                "depends_on": [],
                "stage": "visual_features",
                "key_inputs": ["image_seed", "image_shape", "slide_rect", "texture_cell", "decoder_version"],
            },
            {
                "id": "visual",
                "label": "片区视觉特征提取",
                "scope": "pair",
                "depends_on": ["decode"],
                "stage": "visual_features",
                "key_inputs": ["decode.artifact_hash", "feature_params_version", "tile_grid", "hist_bins"],
            },
            {
                "id": "text",
                "label": "文本词项嵌入",
                "scope": "pair",
                "depends_on": [],
                "stage": "text_embeddings",
                "key_inputs": ["text", "locale", "vocab_version", "tokenizer_version"],
            },
            {
                "id": "align",
                "label": "跨模态对齐打分",
                "scope": "pair",
                "depends_on": ["visual", "text"],
                "stage": "alignment_scores",
                "key_inputs": ["visual.artifact_hash", "text.artifact_hash", "cosine_metric", "vocab_version", "feature_params_version"],
            },
            {
                "id": "ranking",
                "label": "检索排序与指标聚合",
                "scope": "global",
                "depends_on": ["align"],
                "stage": "retrieval_ranking",
                "key_inputs": ["all_align.artifact_hashes", "min_score", "top_k", "vocab_version", "feature_params_version"],
            },
        ]
        return {
            "nodes": nodes,
            "edges": [
                {"from": "decode", "to": "visual"},
                {"from": "visual", "to": "align"},
                {"from": "text", "to": "align"},
                {"from": "align", "to": "ranking"},
            ],
            "stages": list(STAGE_ORDER),
            "tie_break": "score_desc,pair_id_natural_asc,block_index_asc,pair_id",
            "similarity": dict(SIMILARITY_DECLARATION),
        }

    def manifest(self) -> Optional[Dict[str, Any]]:
        raw = read_json(self.manifest_path, error_code=E_STATE_INVALID, field="manifest")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise BusinessError(E_STATE_INVALID, "运行清单格式损坏", field="manifest")
        expected = raw.get("checksum")
        body = {key: value for key, value in raw.items() if key != "checksum"}
        if expected != digest(body):
            raise BusinessError(E_STATE_INVALID, "运行清单校验和不匹配", field="manifest")
        return body

    def _write_manifest(self, body: Dict[str, Any]) -> Dict[str, Any]:
        document = dict(body)
        document["checksum"] = digest(body)
        atomic_write_json(self.manifest_path, document)
        return document

    def hit_index(self) -> Dict[str, Any]:
        value = read_json(
            self.hit_index_path,
            default={"hits": {}},
            error_code=E_STATE_INVALID,
            field="hit_index",
        )
        if not isinstance(value, dict) or not isinstance(value.get("hits"), dict):
            raise BusinessError(E_STATE_INVALID, "命中索引格式损坏", field="hit_index")
        return value

    def latest_retrieval(self) -> Optional[Dict[str, Any]]:
        manifest = self.manifest()
        if manifest is None:
            return None
        ranking_record = manifest.get("nodes", {}).get("ranking", {}).get("__global__")
        if ranking_record is None:
            return None
        entry = self.cache.get(ranking_record["cache_key"])
        return {
            "cache_key": ranking_record["cache_key"],
            "artifact_hash": entry["artifact_hash"],
            "ranking": entry["payload"],
        }

    def snapshots(self) -> Dict[str, Any]:
        result = self.stages.snapshots()
        result["registries"] = {}
        for kind in ("vocab", "features", "retrieval_result"):
            current = self.registry.current(kind)
            result["registries"][kind] = (
                None
                if current is None
                else {
                    "version": current["version"],
                    "checksum": current["checksum"],
                    "payload_checksum": current["payload_checksum"],
                    "revision": current["revision"],
                }
            )
        return result

    # ------------------------------------------------------------------
    # Cache key construction
    # ------------------------------------------------------------------
    @staticmethod
    def _version_doc(document: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "version": int(document["version"]),
            "checksum": document["payload_checksum"],
            "snapshot_checksum": document["checksum"],
            "revision": int(document["revision"]),
        }

    @staticmethod
    def _decode_components(sample: SampleRecord, feature_config: FeatureConfig) -> Dict[str, Any]:
        return {
            "node": "decode",
            "algorithm": DECODER_VERSION,
            "input": {
                "pair_id": sample.pair_id,
                "image_seed": sample.image_seed,
                "image_shape": list(sample.image_shape),
                "slide_rect": list(sample.slide_rect),
                "texture_cell": feature_config.texture_cell,
            },
            "upstream": {},
        }

    @staticmethod
    def _visual_components(
        sample: SampleRecord,
        feature_config: FeatureConfig,
        feature_doc: Dict[str, Any],
        decode_hash: str,
    ) -> Dict[str, Any]:
        return {
            "node": "visual",
            "algorithm": VISUAL_VERSION,
            "input": {
                "pair_id": sample.pair_id,
                "feature_params": feature_config.to_dict(),
                "feature_params_version": feature_doc["version"],
                "feature_params_checksum": feature_doc["payload_checksum"],
            },
            "upstream": {"decode": decode_hash},
        }

    @staticmethod
    def _text_components(
        sample: SampleRecord,
        vocab_doc: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "node": "text",
            "algorithm": TEXT_VERSION,
            "input": {
                "pair_id": sample.pair_id,
                "text": sample.text,
                "locale": sample.locale,
                "vocab_version": vocab_doc["version"],
                "vocab_checksum": vocab_doc["payload_checksum"],
            },
            "upstream": {},
        }

    @staticmethod
    def _align_components(
        sample: SampleRecord,
        feature_doc: Dict[str, Any],
        vocab_doc: Dict[str, Any],
        retrieval_config: RetrievalConfig,
        visual_hash: str,
        text_hash: str,
    ) -> Dict[str, Any]:
        return {
            "node": "align",
            "algorithm": ALIGN_VERSION,
            "input": {
                "pair_id": sample.pair_id,
                "similarity": {
                    "metric": retrieval_config.metric,
                    "tile_reduce": "max",
                    "tie_break": "block_index_ascending",
                },
                "feature_params_version": feature_doc["version"],
                "feature_params_checksum": feature_doc["payload_checksum"],
                "vocab_version": vocab_doc["version"],
                "vocab_checksum": vocab_doc["payload_checksum"],
            },
            "upstream": {"visual": visual_hash, "text": text_hash},
        }

    @staticmethod
    def _ranking_components(
        samples: Sequence[SampleRecord],
        align_hashes: Dict[str, str],
        feature_doc: Dict[str, Any],
        vocab_doc: Dict[str, Any],
        retrieval_config: RetrievalConfig,
    ) -> Dict[str, Any]:
        return {
            "node": "ranking",
            "algorithm": RANK_VERSION,
            "input": {
                "retrieval": retrieval_config.to_dict(),
                "pair_ids": [sample.pair_id for sample in samples],
                "tie_break": "score_desc,pair_id_natural_asc,block_index_asc,pair_id",
                "feature_params_version": feature_doc["version"],
                "feature_params_checksum": feature_doc["payload_checksum"],
                "vocab_version": vocab_doc["version"],
                "vocab_checksum": vocab_doc["payload_checksum"],
            },
            "upstream": {"align": dict(sorted(align_hashes.items()))},
        }

    # ------------------------------------------------------------------
    # Incremental execution
    # ------------------------------------------------------------------
    def prepare_change(self, reason: str) -> Dict[str, Any]:
        """Called before mutating workspace inputs/config; archives old stage run."""
        return self.stages.start_new_run(reason=reason)

    def run(
        self,
        samples: Sequence[SampleRecord],
        vocabulary: Vocabulary,
        feature_config: FeatureConfig,
        retrieval_config: RetrievalConfig,
        max_pixel_bytes: int,
        max_cache_entries: int,
        max_recompute_pairs: int,
        crash_after: Optional[str] = None,
        force_new_run: bool = False,
    ) -> RunOutputs:
        if not samples:
            raise BusinessError(E_INPUT_INVALID, "至少需要一个样本 pair", field="samples")
        for sample in samples:
            tokenize_text(sample.text, vocabulary, sample.pair_id)
        with self.lock:
            vocab_doc = self.registry.current("vocab")
            feature_doc = self.registry.current("features")
            if vocab_doc is None or feature_doc is None:
                raise BusinessError(E_STATE_INVALID, "词表或特征参数快照尚未初始化", field="snapshots")
            if digest(vocabulary.to_dict()) != vocab_doc["payload_checksum"]:
                raise BusinessError(
                    E_STATE_INVALID,
                    "内存词表与当前词表快照不一致",
                    field="vocab_version",
                )
            if digest(feature_config.to_dict()) != feature_doc["payload_checksum"]:
                raise BusinessError(
                    E_STATE_INVALID,
                    "内存特征参数与当前特征快照不一致",
                    field="feature_params_version",
                )

            current_inputs_digest = self._inputs_digest(samples, vocab_doc, feature_doc, retrieval_config)
            manifest = self.manifest()
            recovery = self.stages.recover()
            current_run = self.stages.current_run()
            run_inputs_digest = None if current_run is None else current_run.get("inputs_digest")
            run_inputs_stale = (
                run_inputs_digest is not None
                and run_inputs_digest != current_inputs_digest
                and bool(recovery.get("committed_stages"))
            )
            if (
                force_new_run
                or recovery["run_id"] is None
                or recovery["complete"]
                or run_inputs_stale
            ):
                reason = "forced_new_run" if force_new_run else "new_run"
                self.stages.start_new_run(reason=reason, inputs_digest=current_inputs_digest)
                recovery = self.stages.recover()
            elif run_inputs_digest is None:
                self.stages.bind_inputs_digest(recovery["run_id"], current_inputs_digest)
                recovery = self.stages.recover()

            run_id = recovery["run_id"]
            assert run_id is not None
            recovered_index = -1
            if recovery["last_committed_stage"] is not None:
                recovered_index = STAGE_ORDER.index(recovery["last_committed_stage"])

            affected = self._compute_affected(
                samples,
                manifest,
                vocab_doc,
                feature_doc,
                retrieval_config,
            )
            affected_pair_count = len(affected["pair_ids"])
            if affected_pair_count > max_recompute_pairs:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "单次重算 pair 数超过配置上限",
                    field="max_recompute_pairs",
                    details={
                        "affected_pairs": affected_pair_count,
                        "max_recompute_pairs": max_recompute_pairs,
                    },
                )

            sorted_samples = sorted(samples, key=lambda item: natural_key(item.pair_id))
            pair_ids = [sample.pair_id for sample in sorted_samples]
            nodes: Dict[str, Dict[str, Dict[str, Any]]] = {name: {} for name in self.NODES}
            decoded: Dict[str, Dict[str, Any]] = {}
            visual: Dict[str, Dict[str, Any]] = {}
            text: Dict[str, Dict[str, Any]] = {}
            aligned: Dict[str, Dict[str, Any]] = {}
            recomputed: List[Dict[str, Any]] = []
            reused: List[Dict[str, Any]] = []
            recomputed_pairs: Set[str] = set()

            # Stage 1: decode + visual features
            for sample in sorted_samples:
                validate_pixel_budget(sample.image_shape, max_pixel_bytes, sample.pair_id)
                components = self._decode_components(sample, feature_config)
                key = self.cache.key_for(components)
                entry = self.cache.get(key, allow_missing=True)
                if entry is not None:
                    decoded[sample.pair_id] = entry["payload"]
                    nodes["decode"][sample.pair_id] = self._node_record(key, entry["artifact_hash"], entry["versions"])
                    reused.append({"node": "decode", "pair_id": sample.pair_id, "cache_key": key})
                else:
                    canvas = decode_frame(sample.image_seed, sample.image_shape, sample.slide_rect, feature_config.texture_cell)
                    pixels = canvas.tobytes(order="C")
                    payload = {
                        "algorithm": DECODER_VERSION,
                        "image_seed": sample.image_seed,
                        "image_shape": list(sample.image_shape),
                        "slide_rect": list(sample.slide_rect),
                        "texture_cell": feature_config.texture_cell,
                        "frame_hash": frame_hash(canvas),
                        "pixels_b64": bytes_b64(pixels),
                    }
                    payload["artifact_hash"] = digest(payload)
                    self.cache.ensure_capacity([key], max_cache_entries)
                    self.cache.put(
                        key,
                        "decode",
                        sample.pair_id,
                        components,
                        {"decoder_algorithm": DECODER_VERSION},
                        payload,
                    )
                    payload = self.cache.get(key)["payload"]
                    decoded[sample.pair_id] = payload
                    nodes["decode"][sample.pair_id] = self._node_record(
                        key, payload_checksum(payload), {"decoder_algorithm": DECODER_VERSION}
                    )
                    recomputed.append({"node": "decode", "pair_id": sample.pair_id, "cache_key": key})
                    recomputed_pairs.add(sample.pair_id)

                visual_components = self._visual_components(
                    sample, feature_config, feature_doc, payload_checksum(decoded[sample.pair_id])
                )
                visual_key = self.cache.key_for(visual_components)
                visual_entry = self.cache.get(visual_key, allow_missing=True)
                if visual_entry is not None:
                    visual[sample.pair_id] = visual_entry["payload"]
                    nodes["visual"][sample.pair_id] = self._node_record(
                        visual_key, visual_entry["artifact_hash"], visual_entry["versions"]
                    )
                    reused.append({"node": "visual", "pair_id": sample.pair_id, "cache_key": visual_key})
                else:
                    canvas = np.frombuffer(
                        b64_bytes(decoded[sample.pair_id]["pixels_b64"]), dtype=np.uint8
                    ).reshape(sample.image_shape)
                    visual_payload = extract_visual_features(canvas, sample.slide_rect, feature_config)
                    versions = {
                        "feature_params_version": feature_doc["version"],
                        "feature_params_checksum": feature_doc["payload_checksum"],
                    }
                    self.cache.ensure_capacity([visual_key], max_cache_entries)
                    self.cache.put(
                        visual_key,
                        "visual",
                        sample.pair_id,
                        visual_components,
                        versions,
                        visual_payload,
                    )
                    visual_payload = self.cache.get(visual_key)["payload"]
                    visual[sample.pair_id] = visual_payload
                    nodes["visual"][sample.pair_id] = self._node_record(
                        visual_key, payload_checksum(visual_payload), versions
                    )
                    recomputed.append({"node": "visual", "pair_id": sample.pair_id, "cache_key": visual_key})
                    recomputed_pairs.add(sample.pair_id)

            self._enforce_actual_recompute_limit(recomputed_pairs, max_recompute_pairs)
            stage_items = {
                sample.pair_id: {
                    "decode": nodes["decode"][sample.pair_id],
                    "visual": nodes["visual"][sample.pair_id],
                }
                for sample in sorted_samples
            }
            visual_stage = self._commit_or_verify_stage(
                "visual_features", pair_ids, stage_items, run_id, recovered_index, "visual"
            )
            self._maybe_crash(crash_after, "visual_features")

            # Stage 2: text embeddings
            for sample in sorted_samples:
                components = self._text_components(sample, vocab_doc)
                key = self.cache.key_for(components)
                entry = self.cache.get(key, allow_missing=True)
                if entry is not None:
                    text[sample.pair_id] = entry["payload"]
                    nodes["text"][sample.pair_id] = self._node_record(key, entry["artifact_hash"], entry["versions"])
                    reused.append({"node": "text", "pair_id": sample.pair_id, "cache_key": key})
                else:
                    payload = embed_text(sample.text, vocabulary, int(vocab_doc["version"]), sample.pair_id)
                    versions = {
                        "vocab_version": vocab_doc["version"],
                        "vocab_checksum": vocab_doc["payload_checksum"],
                    }
                    self.cache.ensure_capacity([key], max_cache_entries)
                    self.cache.put(key, "text", sample.pair_id, components, versions, payload)
                    payload = self.cache.get(key)["payload"]
                    text[sample.pair_id] = payload
                    nodes["text"][sample.pair_id] = self._node_record(key, payload_checksum(payload), versions)
                    recomputed.append({"node": "text", "pair_id": sample.pair_id, "cache_key": key})
                    recomputed_pairs.add(sample.pair_id)

            self._enforce_actual_recompute_limit(recomputed_pairs, max_recompute_pairs)
            stage_items = {
                sample.pair_id: {"text": nodes["text"][sample.pair_id]} for sample in sorted_samples
            }
            text_stage = self._commit_or_verify_stage(
                "text_embeddings", pair_ids, stage_items, run_id, recovered_index, "text"
            )
            self._maybe_crash(crash_after, "text_embeddings")

            # Stage 3: alignment scores
            feature_versions = self._version_doc(feature_doc)
            vocab_versions = self._version_doc(vocab_doc)
            for sample in sorted_samples:
                components = self._align_components(
                    sample,
                    feature_doc,
                    vocab_doc,
                    retrieval_config,
                    payload_checksum(visual[sample.pair_id]),
                    payload_checksum(text[sample.pair_id]),
                )
                key = self.cache.key_for(components)
                entry = self.cache.get(key, allow_missing=True)
                if entry is not None:
                    aligned[sample.pair_id] = entry["payload"]
                    nodes["align"][sample.pair_id] = self._node_record(key, entry["artifact_hash"], entry["versions"])
                    reused.append({"node": "align", "pair_id": sample.pair_id, "cache_key": key})
                else:
                    payload = align_from_artifacts(
                        visual[sample.pair_id],
                        text[sample.pair_id],
                        feature_versions,
                        vocab_versions,
                    )
                    versions = {
                        "feature_params_version": feature_doc["version"],
                        "feature_params_checksum": feature_doc["payload_checksum"],
                        "vocab_version": vocab_doc["version"],
                        "vocab_checksum": vocab_doc["payload_checksum"],
                        "similarity": retrieval_config.metric,
                    }
                    self.cache.ensure_capacity([key], max_cache_entries)
                    self.cache.put(key, "align", sample.pair_id, components, versions, payload)
                    payload = self.cache.get(key)["payload"]
                    aligned[sample.pair_id] = payload
                    nodes["align"][sample.pair_id] = self._node_record(key, payload_checksum(payload), versions)
                    recomputed.append({"node": "align", "pair_id": sample.pair_id, "cache_key": key})
                    recomputed_pairs.add(sample.pair_id)

            self._enforce_actual_recompute_limit(recomputed_pairs, max_recompute_pairs)
            stage_items = {
                sample.pair_id: {"align": nodes["align"][sample.pair_id]} for sample in sorted_samples
            }
            align_stage = self._commit_or_verify_stage(
                "alignment_scores", pair_ids, stage_items, run_id, recovered_index, "align"
            )
            self._maybe_crash(crash_after, "alignment_scores")

            # Stage 4: ranking and metrics
            align_hashes = {
                pair_id: payload_checksum(aligned[pair_id]) for pair_id in pair_ids
            }
            ranking_components = self._ranking_components(
                sorted_samples,
                align_hashes,
                feature_doc,
                vocab_doc,
                retrieval_config,
            )
            ranking_key = self.cache.key_for(ranking_components)
            ranking_entry = self.cache.get(ranking_key, allow_missing=True)
            if ranking_entry is not None:
                ranking_payload = ranking_entry["payload"]
                ranking_hash = ranking_entry["artifact_hash"]
                nodes["ranking"]["__global__"] = self._node_record(
                    ranking_key, ranking_hash, ranking_entry["versions"]
                )
                reused.append({"node": "ranking", "pair_id": "__global__", "cache_key": ranking_key})
            else:
                versions = {
                    "feature_params_version": feature_doc["version"],
                    "feature_params_checksum": feature_doc["payload_checksum"],
                    "vocab_version": vocab_doc["version"],
                    "vocab_checksum": vocab_doc["payload_checksum"],
                    "retrieval_config": retrieval_config.to_dict(),
                }
                ranking_payload = build_ranking(
                    [(pair_id, aligned[pair_id]) for pair_id in pair_ids],
                    retrieval_config,
                    versions,
                )
                hits = self._build_hits(
                    ranking_payload,
                    nodes,
                    vocab_doc,
                    feature_doc,
                    ranking_key,
                    pair_ids,
                )
                ranking_payload["hits"] = hits
                ranking_payload["artifact_hash"] = digest(
                    {key: value for key, value in ranking_payload.items() if key != "artifact_hash"}
                )
                self.cache.ensure_capacity([ranking_key], max_cache_entries)
                self.cache.put(
                    ranking_key,
                    "ranking",
                    "__global__",
                    ranking_components,
                    versions,
                    ranking_payload,
                )
                ranking_payload = self.cache.get(ranking_key)["payload"]
                ranking_hash = payload_checksum(ranking_payload)
                nodes["ranking"]["__global__"] = self._node_record(ranking_key, ranking_hash, versions)
                recomputed.append({"node": "ranking", "pair_id": "__global__", "cache_key": ranking_key})
                recomputed_pairs.add("__global__")

            retrieval_payload = {
                "ranking_cache_key": ranking_key,
                "ranking_artifact_hash": ranking_hash,
                "ranking_algorithm": RANK_VERSION,
                "hits": ranking_payload["hits"],
                "metrics": ranking_payload["metrics"],
                "versions": ranking_payload["versions"],
            }
            retrieval_snapshot = self.registry.put("retrieval_result", retrieval_payload)
            hits = ranking_payload["hits"]
            self._merge_hit_index(hits, retrieval_snapshot)

            stage_items = {}
            rank_by_pair = {item["pair_id"]: item for item in ranking_payload["hits"]}
            for pair_id in pair_ids:
                selected = rank_by_pair.get(pair_id)
                stage_items[pair_id] = {
                    "ranking": nodes["ranking"]["__global__"],
                    "rank": None if selected is None else selected["rank"],
                    "hit_id": None if selected is None else selected["hit_id"],
                }
            ranking_stage = self._commit_or_verify_stage(
                "retrieval_ranking", pair_ids, stage_items, run_id, recovered_index, "ranking"
            )

            manifest_body = {
                "schema_version": 1,
                "run_id": run_id,
                "inputs_digest": current_inputs_digest,
                "samples": {sample.pair_id: sample.to_dict() for sample in sorted_samples},
                "config": {
                    "vocab": self._version_doc(vocab_doc),
                    "features": self._version_doc(feature_doc),
                    "retrieval": retrieval_config.to_dict(),
                },
                "nodes": nodes,
                "ranking_key": ranking_key,
                "ranking_artifact_hash": ranking_hash,
                "retrieval_snapshot": {
                    "version": retrieval_snapshot["version"],
                    "checksum": retrieval_snapshot["checksum"],
                },
                "last_result": {
                    "affected": affected,
                    "recomputed_nodes": recomputed,
                    "reused_nodes": reused,
                },
            }
            self._write_manifest(manifest_body)
            self._maybe_crash(crash_after, "retrieval_ranking")

            stage_views = {
                "visual_features": self._stage_view(visual_stage),
                "text_embeddings": self._stage_view(text_stage),
                "alignment_scores": self._stage_view(align_stage),
                "retrieval_ranking": self._stage_view(ranking_stage),
            }
            key_preview = {
                "sample_pair_id": pair_ids[0],
                "decode": nodes["decode"][pair_ids[0]]["cache_key"],
                "visual": nodes["visual"][pair_ids[0]]["cache_key"],
                "text": nodes["text"][pair_ids[0]]["cache_key"],
                "align": nodes["align"][pair_ids[0]]["cache_key"],
                "ranking": ranking_key,
            }
            return RunOutputs(
                run_id=run_id,
                recovery=self.stages.recover(),
                affected=affected,
                recomputed_nodes=recomputed,
                reused_nodes=reused,
                ranking=ranking_payload,
                retrieval_snapshot=retrieval_snapshot,
                stages=stage_views,
                cache_key_preview=key_preview,
                degradation=[],
            )

    # ------------------------------------------------------------------
    # Change analysis
    # ------------------------------------------------------------------
    @staticmethod
    def _inputs_digest(
        samples: Sequence[SampleRecord],
        vocab_doc: Dict[str, Any],
        feature_doc: Dict[str, Any],
        retrieval_config: RetrievalConfig,
    ) -> str:
        return digest(
            {
                "samples": [sample.to_dict() for sample in sorted(samples, key=lambda item: natural_key(item.pair_id))],
                "vocab_payload_checksum": vocab_doc["payload_checksum"],
                "feature_payload_checksum": feature_doc["payload_checksum"],
                "retrieval": retrieval_config.to_dict(),
            }
        )

    def _compute_affected(
        self,
        samples: Sequence[SampleRecord],
        manifest: Optional[Dict[str, Any]],
        vocab_doc: Dict[str, Any],
        feature_doc: Dict[str, Any],
        retrieval_config: RetrievalConfig,
    ) -> Dict[str, Any]:
        affected: Dict[str, Set[str]] = {node: set() for node in self.LOCAL_NODES}
        initial_run = manifest is None
        old_samples = {} if manifest is None else manifest.get("samples", {})
        old_config = {} if manifest is None else manifest.get("config", {})
        old_vocab = old_config.get("vocab", {})
        old_features = old_config.get("features", {})
        old_retrieval = old_config.get("retrieval", {})
        old_feature_payload = old_features.get("checksum")
        old_vocab_payload = old_vocab.get("checksum")
        feature_changed = not initial_run and old_feature_payload != feature_doc["payload_checksum"]
        vocab_changed = not initial_run and old_vocab_payload != vocab_doc["payload_checksum"]
        retrieval_changed = not initial_run and old_retrieval != retrieval_config.to_dict()
        metric_changed = not initial_run and old_retrieval.get("metric") != retrieval_config.metric

        current_ids = {sample.pair_id for sample in samples}
        old_ids = set(old_samples)
        pair_set_changed = current_ids != old_ids
        for sample in samples:
            old = old_samples.get(sample.pair_id)
            pair_local_all = old is None
            decode_changed = pair_local_all or any(
                old.get(field) != sample.to_dict().get(field)
                for field in ("image_seed", "image_shape", "slide_rect")
            )
            visual_changed = pair_local_all or decode_changed or feature_changed
            text_changed = pair_local_all or vocab_changed or any(
                old.get(field) != sample.to_dict().get(field) for field in ("text", "locale")
            )
            align_changed = pair_local_all or visual_changed or text_changed or metric_changed
            if decode_changed:
                affected["decode"].add(sample.pair_id)
            if visual_changed:
                affected["visual"].add(sample.pair_id)
            if text_changed:
                affected["text"].add(sample.pair_id)
            if align_changed:
                affected["align"].add(sample.pair_id)

        local_union: Set[str] = set()
        for values in affected.values():
            local_union.update(values)
        ranking_affected = bool(local_union) or retrieval_changed or pair_set_changed
        return {
            "decode": sorted(affected["decode"], key=natural_key),
            "visual": sorted(affected["visual"], key=natural_key),
            "text": sorted(affected["text"], key=natural_key),
            "align": sorted(affected["align"], key=natural_key),
            "ranking": ["__global__"] if ranking_affected else [],
            "pair_ids": sorted(local_union, key=natural_key),
            "global_nodes": ["ranking"] if ranking_affected else [],
            "reasons": {
                "initial_run": initial_run,
                "feature_params_changed": feature_changed,
                "vocab_changed": vocab_changed,
                "retrieval_config_changed": retrieval_changed,
                "similarity_metric_changed": metric_changed,
                "pair_set_changed": pair_set_changed,
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _node_record(key: str, artifact_hash: str, versions: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "cache_key": key,
            "artifact_hash": artifact_hash,
            "versions": versions,
        }

    @staticmethod
    def _stage_view(document: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "run_id": document["run_id"],
            "version": document["version"],
            "completed_pair_ids": document["completed_pair_ids"],
            "checksum": document["checksum"],
            "committed_ns": document.get("committed_ns"),
        }

    def _enforce_actual_recompute_limit(self, recomputed_pairs: Set[str], limit: int) -> None:
        pair_count = len({pair_id for pair_id in recomputed_pairs if pair_id != "__global__"})
        if pair_count > limit:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "实际重算 pair 数超过配置上限",
                field="max_recompute_pairs",
                details={"recomputed_pairs": pair_count, "max_recompute_pairs": limit},
            )

    def _commit_or_verify_stage(
        self,
        stage: str,
        pair_ids: Sequence[str],
        items: Dict[str, Any],
        run_id: str,
        recovered_index: int,
        required_item: str,
    ) -> Dict[str, Any]:
        stage_index = STAGE_ORDER.index(stage)
        if restored := self.stages.load_stage(run_id, stage):
            if set(restored["completed_pair_ids"]) != set(pair_ids):
                raise BusinessError(
                    E_STATE_INVALID,
                    "恢复阶段缺少 pair，拒绝继续",
                    field=stage,
                    details={"stage": stage},
                )
            for pair_id, item in items.items():
                if required_item not in item:
                    raise BusinessError(E_STATE_INVALID, "阶段产物缺失", pair_id=pair_id, field=required_item)
                restored_item = restored["items"].get(pair_id, {})
                if required_item in restored_item:
                    old_hash = restored_item[required_item].get("artifact_hash")
                    new_hash = item[required_item].get("artifact_hash")
                    if old_hash != new_hash:
                        raise BusinessError(
                            E_STATE_INVALID,
                            "恢复阶段产物哈希不一致",
                            pair_id=pair_id,
                            field=stage,
                        )
            return restored
        if stage_index <= recovered_index:
            raise BusinessError(E_STATE_INVALID, "阶段恢复顺序不连续", field=stage)
        if set(items) != set(pair_ids):
            raise BusinessError(
                E_STATE_INVALID,
                "阶段未覆盖全部 pair，禁止提交",
                field=stage,
                details={"stage": stage, "completed": len(items), "required": len(pair_ids)},
            )
        return self.stages.commit_stage(
            stage,
            pair_ids,
            items,
            run_id=run_id,
            lineage={"node": required_item, "pair_count": len(pair_ids)},
        )

    @staticmethod
    def _maybe_crash(stage_to_crash: Optional[str], committed_stage: str) -> None:
        if stage_to_crash == committed_stage:
            raise ForcedTermination(committed_stage)

    def _build_hits(
        self,
        ranking_payload: Dict[str, Any],
        nodes: Dict[str, Dict[str, Dict[str, Any]]],
        vocab_doc: Dict[str, Any],
        feature_doc: Dict[str, Any],
        ranking_key: str,
        pair_ids: Sequence[str],
    ) -> List[Dict[str, Any]]:
        hits = []
        for item in ranking_payload["hits"]:
            pair_id = item["pair_id"]
            core = {
                "pair_id": pair_id,
                "rank": item["rank"],
                "score": item["score"],
                "ranking_cache_key": ranking_key,
                "align_cache_key": nodes["align"][pair_id]["cache_key"],
            }
            hit_id = "hit-" + digest(core)[:24]
            source_path = [
                {"node": "decode", "pair_id": pair_id, **nodes["decode"][pair_id]},
                {"node": "visual", "pair_id": pair_id, **nodes["visual"][pair_id]},
                {"node": "text", "pair_id": pair_id, **nodes["text"][pair_id]},
                {"node": "align", "pair_id": pair_id, **nodes["align"][pair_id]},
                {
                    "node": "ranking",
                    "pair_id": "__global__",
                    "cache_key": ranking_key,
                    "versions": ranking_payload["versions"],
                },
            ]
            versions = {
                "vocab": self._version_doc(vocab_doc),
                "feature_params": self._version_doc(feature_doc),
            }
            hit = {
                "hit_id": hit_id,
                "pair_id": pair_id,
                "rank": item["rank"],
                "score": item["score"],
                "best_block_index": item["best_block_index"],
                "alignment_artifact_hash": item["alignment_artifact_hash"],
                "ranking_cache_key": ranking_key,
                "source_node_path": source_path,
                "cache_keys": {
                    "decode": nodes["decode"][pair_id]["cache_key"],
                    "visual": nodes["visual"][pair_id]["cache_key"],
                    "text": nodes["text"][pair_id]["cache_key"],
                    "align": nodes["align"][pair_id]["cache_key"],
                    "ranking": ranking_key,
                },
                "versions": versions,
                "vocab_version": vocab_doc["version"],
                "vocab_checksum": vocab_doc["payload_checksum"],
                "feature_params_version": feature_doc["version"],
                "feature_params_checksum": feature_doc["payload_checksum"],
                "similarity": dict(SIMILARITY_DECLARATION),
                "tie_break": ranking_payload["tie_break"],
            }
            hits.append(hit)
        ranking_payload["hits"] = hits
        return hits

    def _merge_hit_index(self, hits: Sequence[Dict[str, Any]], retrieval_snapshot: Dict[str, Any]) -> None:
        index = self.hit_index()
        for hit in hits:
            index["hits"][hit["hit_id"]] = {
                "pair_id": hit["pair_id"],
                "rank": hit["rank"],
                "score": hit["score"],
                "retrieval_cache_key": hit["ranking_cache_key"],
                "retrieval_snapshot_version": retrieval_snapshot["version"],
                "vocab_version": hit["vocab_version"],
                "feature_params_version": hit["feature_params_version"],
            }
        atomic_write_json(self.hit_index_path, index)

    # ------------------------------------------------------------------
    # Tracing and replay
    # ------------------------------------------------------------------
    def query_cache(self, node: str, pair_id: str) -> Dict[str, Any]:
        if node not in self.NODES:
            raise BusinessError(E_INPUT_INVALID, "未知节点", pair_id=pair_id, field="node")
        manifest = self.manifest()
        if manifest is None:
            raise BusinessError(E_CACHE_MISS, "尚无运行清单", pair_id=pair_id, field=node)
        if node == "ranking":
            record = manifest["nodes"]["ranking"].get("__global__")
            if record is None:
                raise BusinessError(E_CACHE_MISS, "尚无检索排序乘积", pair_id=pair_id, field=node)
            description = self.cache.describe(record["cache_key"])
            ranking_payload = self.cache.get(record["cache_key"])["payload"]
            facet = next(
                (item for item in ranking_payload["hits"] + ranking_payload["eligible"] if item["pair_id"] == pair_id),
                None,
            )
            description["pair_facet"] = facet
            description["pair_facet_hash"] = None if facet is None else digest(facet)
            return description
        record = manifest["nodes"].get(node, {}).get(pair_id)
        if record is None:
            raise BusinessError(E_CACHE_MISS, "该 pair 无对应节点缓存", pair_id=pair_id, field=node)
        return self.cache.describe(record["cache_key"])

    def trace_hit(self, hit_id: str) -> Dict[str, Any]:
        index = self.hit_index()
        indexed = index["hits"].get(hit_id)
        if indexed is None:
            raise BusinessError(E_HIT_NOT_FOUND, "命中记录不存在", field="hit_id", details={"hit_id": hit_id})
        retrieval_entry = self.cache.get(indexed["retrieval_cache_key"])
        ranking = retrieval_entry["payload"]
        hit = next((item for item in ranking.get("hits", []) if item.get("hit_id") == hit_id), None)
        if hit is None:
            raise BusinessError(E_HIT_NOT_FOUND, "检索快照中缺少该命中", field="hit_id", details={"hit_id": hit_id})
        status = self._lineage_status(hit)
        return {
            "hit": hit,
            "index": indexed,
            "lineage": status,
            "ranking_order": [
                {"rank": item["rank"], "pair_id": item["pair_id"], "score": item["score"]}
                for item in ranking.get("hits", [])
            ],
        }

    def _lineage_status(self, hit: Dict[str, Any]) -> Dict[str, Any]:
        current_vocab = self.registry.current("vocab")
        current_features = self.registry.current("features")
        broken = []
        dependencies = [
            ("vocab", hit["versions"]["vocab"], current_vocab),
            ("feature_params", hit["versions"]["feature_params"], current_features),
        ]
        for kind, recorded, current in dependencies:
            if current is None:
                broken.append(
                    {
                        "kind": kind,
                        "recorded_version": recorded.get("version"),
                        "recorded_checksum": recorded.get("checksum"),
                        "current_version": None,
                        "current_checksum": None,
                        "recorded_revision": recorded.get("revision", -1),
                        "current_revision": -1,
                    }
                )
                continue
            if (
                int(recorded.get("version", -1)) != int(current["version"])
                or recorded.get("checksum") != current["payload_checksum"]
            ):
                broken.append(
                    {
                        "kind": kind,
                        "recorded_version": recorded.get("version"),
                        "recorded_checksum": recorded.get("checksum"),
                        "current_version": current["version"],
                        "current_checksum": current["payload_checksum"],
                        "recorded_revision": recorded.get("revision", -1),
                        "current_revision": current["revision"],
                    }
                )
        broken.sort(key=lambda item: (item["current_revision"], item["kind"]))
        return {"ok": not broken, "broken_dependencies": broken, "earliest_invalid_version": broken[0] if broken else None}

    def replay_hit(self, hit_id: str) -> Dict[str, Any]:
        traced = self.trace_hit(hit_id)
        lineage = traced["lineage"]
        if not lineage["ok"]:
            raise BusinessError(
                E_LINEAGE_BROKEN,
                "命中缓存键依赖的版本已变化",
                field="lineage",
                details={
                    "hit_id": hit_id,
                    "broken_dependencies": lineage["broken_dependencies"],
                    "earliest_invalid_version": lineage["earliest_invalid_version"],
                },
            )
        hit = traced["hit"]
        keys = hit["cache_keys"]
        visual = self.cache.get(keys["visual"])["payload"]
        text = self.cache.get(keys["text"])["payload"]
        align_entry = self.cache.get(keys["align"])
        expected_align = align_entry["payload"]
        if expected_align.get("artifact_hash") != hit["alignment_artifact_hash"]:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "对齐缓存产物哈希与命中记录不一致",
                field="alignment_artifact_hash",
                details={"hit_id": hit_id},
            )
        text_vector = vector_from_text_artifact(text)
        recomputed_score = -2.0
        recomputed_block = -1
        for tile in visual["tiles"]:
            tile_score = cosine_score(text_vector, b64_f64(tile["vector_f64_b64"]))
            block_index = int(tile["block_index"])
            if tile_score > recomputed_score or (
                tile_score == recomputed_score and block_index < recomputed_block
            ):
                recomputed_score = tile_score
                recomputed_block = block_index
        recomputed_text = float_text(recomputed_score)
        if recomputed_text != expected_align["score"] or recomputed_text != hit["score"]:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "重放分数与缓存记录不一致",
                field="score",
                details={
                    "hit_id": hit_id,
                    "expected": expected_align["score"],
                    "recomputed": recomputed_text,
                },
            )
        if (
            recomputed_block != int(expected_align["best_block_index"])
            or recomputed_block != int(hit["best_block_index"])
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "重放最佳块索引与缓存记录不一致",
                field="best_block_index",
                details={
                    "hit_id": hit_id,
                    "expected": hit["best_block_index"],
                    "recomputed": recomputed_block,
                },
            )
        ranking = self.cache.get(hit["ranking_cache_key"])["payload"]
        current_hit = next((item for item in ranking["hits"] if item["hit_id"] == hit_id), None)
        if current_hit is None or current_hit["rank"] != hit["rank"]:
            raise BusinessError(E_CACHE_CORRUPT, "重放排序与检索快照不一致", field="rank")
        replay_config = RetrievalConfig.from_dict(ranking["retrieval"])
        replay_candidates = [
            (
                item["pair_id"],
                {
                    "score": item["score"],
                    "artifact_hash": item["alignment_artifact_hash"],
                    "best_block_index": item["best_block_index"],
                },
            )
            for item in ranking["eligible"]
        ]
        rebuilt = build_ranking(replay_candidates, replay_config, ranking["versions"])
        rebuilt_order = [
            (item["rank"], item["pair_id"], item["score"]) for item in rebuilt["hits"]
        ]
        stored_order = [
            (item["rank"], item["pair_id"], item["score"]) for item in ranking["hits"]
        ]
        if rebuilt_order != stored_order:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "重放排序无法复现检索快照顺序",
                field="ranking_order",
                details={"hit_id": hit_id},
            )
        rebuilt_eligible = [
            (item["pair_id"], item["score"], item["best_block_index"])
            for item in rebuilt["eligible"]
        ]
        stored_eligible = [
            (item["pair_id"], item["score"], item["best_block_index"])
            for item in ranking["eligible"]
        ]
        if rebuilt_eligible != stored_eligible:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "重放无法复现完整候选排序",
                field="eligible_order",
                details={"hit_id": hit_id},
            )
        return {
            "ok": True,
            "hit_id": hit_id,
            "pair_id": hit["pair_id"],
            "score": hit["score"],
            "recomputed_score": recomputed_text,
            "best_block_index": hit["best_block_index"],
            "recomputed_block_index": recomputed_block,
            "rank": hit["rank"],
            "same_score": True,
            "same_rank": True,
            "same_order": True,
            "ranking_order": [
                {"rank": item["rank"], "pair_id": item["pair_id"], "score": item["score"]}
                for item in ranking["hits"]
            ],
            "source_node_path": hit["source_node_path"],
            "versions": hit["versions"],
        }

    # ------------------------------------------------------------------
    # Self-check
    # ------------------------------------------------------------------
    def self_check(self) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, details: Optional[Dict[str, Any]] = None) -> None:
            checks.append({"name": name, "ok": bool(ok), "details": details or {}})
            if not ok:
                errors.append({"check": name, "details": details or {}})

        cache_report = self.cache.verify_all()
        add("cache_checksums", cache_report["ok"], cache_report)
        try:
            recovery = self.stages.recover()
            add("stage_snapshots", True, {"last_committed_stage": recovery["last_committed_stage"], "complete": recovery["complete"]})
        except BusinessError as exc:
            add("stage_snapshots", False, exc.to_dict()["error"])

        manifest = None
        try:
            manifest = self.manifest()
            add("manifest_checksum", True)
        except BusinessError as exc:
            add("manifest_checksum", False, exc.to_dict()["error"])

        if manifest is not None:
            missing = []
            mismatched = []
            for node, records in manifest.get("nodes", {}).items():
                for pair_id, record in records.items():
                    try:
                        entry = self.cache.get(record["cache_key"])
                        if entry["artifact_hash"] != record["artifact_hash"]:
                            mismatched.append({"node": node, "pair_id": pair_id})
                    except BusinessError:
                        missing.append({"node": node, "pair_id": pair_id})
            add("manifest_cache_links", not missing and not mismatched, {"missing": missing, "mismatched": mismatched})

            vocab = self.registry.current("vocab")
            features = self.registry.current("features")
            expected_vocab = manifest.get("config", {}).get("vocab", {}).get("checksum")
            expected_features = manifest.get("config", {}).get("features", {}).get("checksum")
            add(
                "cross_module_versions",
                vocab is not None
                and features is not None
                and vocab["payload_checksum"] == expected_vocab
                and features["payload_checksum"] == expected_features,
                {
                    "vocab": None if vocab is None else vocab["version"],
                    "features": None if features is None else features["version"],
                },
            )
            try:
                retrieval = self.latest_retrieval()
                ranking_record = manifest["nodes"]["ranking"]["__global__"]
                add(
                    "ranking_link",
                    retrieval is not None and retrieval["artifact_hash"] == ranking_record["artifact_hash"],
                )
            except BusinessError as exc:
                add("ranking_link", False, exc.to_dict()["error"])
            retrieval_snapshot = self.registry.current("retrieval_result")
            add(
                "retrieval_snapshot_link",
                retrieval_snapshot is not None
                and retrieval_snapshot["payload"].get("ranking_cache_key") == manifest.get("ranking_key")
                and retrieval_snapshot["payload"].get("ranking_artifact_hash")
                == manifest.get("ranking_artifact_hash"),
                {}
                if retrieval_snapshot is None
                else {
                    "version": retrieval_snapshot["version"],
                    "ranking_cache_key": retrieval_snapshot["payload"].get("ranking_cache_key"),
                },
            )
            missing_hits = []
            for hit_id, indexed in self.hit_index().get("hits", {}).items():
                try:
                    self.cache.get(indexed["retrieval_cache_key"])
                except BusinessError:
                    missing_hits.append(hit_id)
            add("hit_index_links", not missing_hits, {"missing_hits": missing_hits})
        return {"ok": not errors, "checks": checks, "errors": errors}
