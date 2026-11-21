from __future__ import annotations

import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .cache import CacheStore, build_key_components, key_from_components
from .canonical import array_digest, pretty_json_bytes, sha256_bytes, sha256_json
from .config import (
    codebook_blocks_by_id,
    codebook_memory_estimate,
    codebook_summary,
    config_digest,
    config_semantic_view,
    resource_limits,
    sample_digest,
    sample_image_fields,
    sample_text_fields,
    samples_digest,
)
from .encoders import (
    align_embeddings,
    cosine_scores,
    encode_image,
    encode_text,
    quantize_vectors,
)
from .errors import PipelineError
from .graph import GraphSpec
from .samples import InputBundle
from .state import SNAPSHOT_SCHEMA, StateStore

RESULT_SCHEMA = "cm-retrieval-results-v1"


@dataclass
class Artifact:
    instance: str
    node_type: str
    pair_id: str | None
    key: str
    product_hash: str
    arrays: dict[str, np.ndarray]
    meta: dict[str, Any]


def _worker_encode(task: tuple[str, dict[str, Any]]) -> dict[str, np.ndarray]:
    kind, payload = task
    if kind == "image":
        return encode_image(payload["sample"], payload["config"])
    if kind == "text":
        return encode_text(payload["sample"], payload["vocab"], payload["config"])
    raise ValueError(f"Unknown encode task: {kind}")


def _type_node(graph: GraphSpec, node_type: str) -> str:
    for name in graph.topological_order():
        if graph.nodes[name]["type"] == node_type:
            return name
    raise PipelineError(
        "E_CONFIG_INVALID",
        f"Graph has no {node_type} node",
        stage="graph",
        field="graph.nodes",
    )


def _shard_for_pair(pair_id: str, num_shards: int) -> int:
    return int(sha256_bytes(pair_id.encode("utf-8"))[:16], 16) % num_shards


def _lineage(
    *,
    vocab: dict[str, Any],
    codebook: dict[str, Any],
    config: dict[str, Any],
    samples: list[dict[str, Any]],
    graph: GraphSpec,
) -> dict[str, Any]:
    return {
        "vocab_version": int(vocab["version"]),
        "vocab_checksum": vocab["checksum"],
        "codebook_version": int(codebook["version"]),
        "codebook_checksum": codebook["checksum"],
        "config_digest": config_digest(config),
        "samples_digest": samples_digest(samples),
        "graph_digest": graph.digest(),
    }


class PipelineEngine:
    def __init__(
        self,
        bundle: InputBundle,
        *,
        cache_root: str | Path,
        state_root: str | Path,
        workers: int = 1,
    ) -> None:
        self.bundle = bundle
        self.cache = CacheStore(cache_root)
        self.state = StateStore(state_root)
        self.workers = max(1, int(workers))
        self.graph = GraphSpec.from_config(bundle.config)
        self.samples = sorted(bundle.samples, key=lambda item: item["pair_id"])
        self.samples_by_id = {sample["pair_id"]: sample for sample in self.samples}
        self.limits = resource_limits(bundle.config)
        self._recomputed: list[dict[str, Any]] = []
        self._reused: list[dict[str, Any]] = []
        self._degraded: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ API
    def build_full(self, *, report_path: str | Path | None = None) -> dict[str, Any]:
        with self.state.commit_lock():
            state = self.state.read_state()
            if int(state["current_version"]) > 0:
                raise PipelineError(
                    "E_SNAPSHOT_ROLLBACK",
                    "Full build cannot overwrite an existing committed state",
                    stage="commit",
                    field="version",
                    details={"current": int(state["current_version"])},
                )
            snapshot, report = self._run_build(
                kind="full",
                version=1,
                previous_snapshot=None,
                expected_version=0,
            )
            self.state.commit_snapshot(snapshot, expected_version=0)
        if report_path is not None:
            self.export_report(report, report_path)
        return report

    def build_incremental(
        self,
        *,
        expected_version: int | None = None,
        target_version: int | None = None,
        report_path: str | Path | None = None,
    ) -> dict[str, Any]:
        with self.state.commit_lock():
            state = self.state.read_state()
            current = int(state["current_version"])
            if current <= 0:
                raise PipelineError(
                    "E_SNAPSHOT_ROLLBACK",
                    "Incremental build requires an existing snapshot",
                    stage="commit",
                    field="version",
                )
            if expected_version is None:
                expected_version = current
            if int(expected_version) != current:
                raise PipelineError(
                    "E_SNAPSHOT_ROLLBACK",
                    f"Cannot base an incremental commit on version {expected_version}; "
                    f"current version is {current}",
                    stage="commit",
                    field="version",
                    details={"expected": int(expected_version), "current": current},
                )
            if target_version is None:
                target_version = current + 1
            if int(target_version) <= current:
                raise PipelineError(
                    "E_SNAPSHOT_ROLLBACK",
                    f"Cannot roll state back from {current} to {target_version}",
                    stage="commit",
                    field="version",
                    details={"current": current, "target": int(target_version)},
                )
            previous = self.state.load_snapshot(current)
            snapshot, report = self._run_build(
                kind="incremental",
                version=int(target_version),
                previous_snapshot=previous,
                expected_version=current,
            )
            self.state.commit_snapshot(snapshot, expected_version=current)
        if report_path is not None:
            self.export_report(report, report_path)
        return report

    @staticmethod
    def export_report(report: dict[str, Any], path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pretty_json_bytes(report))

    # ------------------------------------------------------------ build core
    def _run_build(
        self,
        *,
        kind: str,
        version: int,
        previous_snapshot: dict[str, Any] | None,
        expected_version: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._recomputed = []
        self._reused = []
        self._degraded = []
        codebook = self.bundle.codebook
        effective_blocks, effective_codebook = self._reconcile_codebook(
            previous_snapshot
        )
        self._check_versions(previous_snapshot)
        self._check_resource_estimates(previous_snapshot)
        lineage = _lineage(
            vocab=self.bundle.vocab,
            codebook=effective_codebook,
            config=self.bundle.config,
            samples=self.samples,
            graph=self.graph,
        )
        image_artifacts, text_artifacts = self._build_encoders(lineage)
        align_artifacts = self._build_align(image_artifacts, text_artifacts, lineage)
        index_artifacts = self._build_index(
            align_artifacts,
            effective_blocks,
            effective_codebook,
            lineage,
        )
        retrieval_artifacts = self._build_retrieval(
            align_artifacts,
            index_artifacts,
            effective_codebook,
            lineage,
            image_artifacts,
            text_artifacts,
        )
        self._validate_retrieval_artifacts(
            retrieval_artifacts,
            align_artifacts,
            index_artifacts,
            effective_blocks,
        )
        manifest_artifact, result_bytes = self._build_result_manifest(
            retrieval_artifacts,
            lineage,
        )
        total_cache_entries = self.cache.count()
        if total_cache_entries > self.limits["max_cache_entries"]:
            raise PipelineError(
                "E_RESOURCE_LIMIT",
                "Cache entry limit exceeded",
                stage="resource",
                field="resources.max_cache_entries",
                details={
                    "observed": total_cache_entries,
                    "limit": self.limits["max_cache_entries"],
                    "category": "cache_entries",
                },
            )
        nodes: dict[str, dict[str, Any]] = {}
        for artifact in (
            image_artifacts
            + text_artifacts
            + align_artifacts
            + index_artifacts
            + retrieval_artifacts
        ):
            nodes[artifact.instance] = {
                "type": artifact.node_type,
                "pair_id": artifact.pair_id,
                "key": artifact.key,
                "product_hash": artifact.product_hash,
            }
        result_hash = sha256_bytes(result_bytes)
        snapshot_id = f"snap-v{version:06d}-{result_hash[:16]}"
        index_blocks: dict[str, dict[str, Any]] = {}
        for artifact in index_artifacts:
            block_id = int(artifact.meta["block_id"])
            index_blocks[str(block_id)] = {
                "key": artifact.key,
                "product_hash": artifact.product_hash,
                "pair_ids": list(artifact.meta["pair_ids"]),
                "codebook_block_version": int(
                    effective_blocks[block_id]["version"]
                ),
            }
        recomputed_ids = [item["instance"] for item in self._recomputed]
        reused_ids = [item["instance"] for item in self._reused]
        all_instances = sorted(nodes)
        recomputed_pairs = sorted(
            {
                item["pair_id"]
                for item in self._recomputed
                if item.get("pair_id") is not None
            }
        )
        reused_pairs = sorted(
            {
                item["pair_id"]
                for item in self._reused
                if item.get("pair_id") is not None
            }
        )
        recomputed_blocks = sorted(
            {
                int(item["instance"].rsplit("block-", 1)[1])
                for item in self._recomputed
                if item["node_type"] == "index_build"
            }
        )
        reused_blocks = sorted(
            {
                int(item["instance"].rsplit("block-", 1)[1])
                for item in self._reused
                if item["node_type"] == "index_build"
            }
        )
        affected_pairs = self._affected_pairs(
            previous_snapshot,
            recomputed_pairs,
            recomputed_blocks,
            index_blocks,
        )
        if len(recomputed_pairs) > self.limits["max_recompute_pairs"]:
            self._resource_or_degrade(
                "pair_recompute_pairs",
                "resources.max_recompute_pairs",
                len(recomputed_pairs),
                self.limits["max_recompute_pairs"],
            )
        snapshot = {
            "schema": SNAPSHOT_SCHEMA,
            "snapshot_id": snapshot_id,
            "version": version,
            "kind": kind,
            "previous_version": (
                int(previous_snapshot["version"]) if previous_snapshot else None
            ),
            "lineage": lineage,
            "config": config_semantic_view(self.bundle.config),
            "vocab": {
                "version": int(self.bundle.vocab["version"]),
                "checksum": self.bundle.vocab["checksum"],
            },
            "codebook": effective_codebook,
            "samples": self.samples,
            "nodes": nodes,
            "index_blocks": index_blocks,
            "retrieval_manifest": {
                "instance": manifest_artifact.instance,
                "key": manifest_artifact.key,
                "product_hash": manifest_artifact.product_hash,
                "results_sha256": result_hash,
                "results_size": len(result_bytes),
            },
            "recomputed_nodes": recomputed_ids,
            "reused_nodes": reused_ids,
            "all_node_instances": all_instances,
            "recomputed_pair_ids": recomputed_pairs,
            "reused_pair_ids": reused_pairs,
            "affected_pair_ids": affected_pairs,
            "recomputed_index_blocks": recomputed_blocks,
            "reused_index_blocks": reused_blocks,
            "degraded": self._degraded,
        }
        report = {
            "schema": "cm-build-report-v1",
            "build_kind": kind,
            "snapshot_id": snapshot_id,
            "snapshot_version": version,
            "previous_version": snapshot["previous_version"],
            "lineage": lineage,
            "affected_pair_ids": affected_pairs,
            "recomputed_pair_ids": recomputed_pairs,
            "reused_pair_ids": reused_pairs,
            "recomputed_nodes": sorted(
                self._recomputed, key=lambda item: item["instance"]
            ),
            "reused_nodes": sorted(self._reused, key=lambda item: item["instance"]),
            "recomputed_index_blocks": recomputed_blocks,
            "reused_index_blocks": reused_blocks,
            "degraded": list(self._degraded),
            "cache": {
                "entries": total_cache_entries,
                "limits": self.limits,
            },
            "retrieval": {
                "manifest_key": manifest_artifact.key,
                "manifest_product_hash": manifest_artifact.product_hash,
                "results_sha256": result_hash,
                "results_size": len(result_bytes),
                "query_count": len(retrieval_artifacts),
                "hit_count": sum(
                    len(artifact.meta.get("hits", []))
                    for artifact in retrieval_artifacts
                ),
            },
        }
        return snapshot, report

    # ---------------------------------------------------------- invalidation
    def _check_versions(self, previous_snapshot: dict[str, Any] | None) -> None:
        if previous_snapshot is None:
            return
        old_vocab = previous_snapshot["vocab"]
        new_vocab = self.bundle.vocab
        if int(new_vocab["version"]) < int(old_vocab["version"]):
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                "Vocabulary version rollback is not allowed",
                stage="vocab",
                field="version",
                details={
                    "current": int(old_vocab["version"]),
                    "requested": int(new_vocab["version"]),
                },
            )
        if (
            int(new_vocab["version"]) == int(old_vocab["version"])
            and new_vocab["checksum"] != old_vocab["checksum"]
        ):
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                "Vocabulary content changed without a version increase",
                stage="vocab",
                field="version",
                details={
                    "version": int(new_vocab["version"]),
                    "old_checksum": old_vocab["checksum"],
                    "new_checksum": new_vocab["checksum"],
                },
            )

    def _reconcile_codebook(
        self,
        previous_snapshot: dict[str, Any] | None,
    ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        codebook = self.bundle.codebook
        declared = codebook_blocks_by_id(codebook)
        if previous_snapshot is None:
            effective = {block_id: dict(block) for block_id, block in declared.items()}
            return effective, codebook_summary(codebook)
        old = previous_snapshot["codebook"]
        old_blocks = {
            int(block["block_id"]): dict(block) for block in old["blocks"]
        }
        if int(codebook["version"]) < int(old["version"]):
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                "Codebook version rollback is not allowed",
                stage="codebook",
                field="version",
                details={
                    "current": int(old["version"]),
                    "requested": int(codebook["version"]),
                },
            )
        if int(codebook["version"]) == int(old["version"]):
            if codebook["checksum"] != old["checksum"]:
                raise PipelineError(
                    "E_SNAPSHOT_ROLLBACK",
                    "Codebook content changed without a version increase",
                    stage="codebook",
                    field="version",
                    details={
                        "version": int(codebook["version"]),
                        "old_checksum": old["checksum"],
                        "new_checksum": codebook["checksum"],
                    },
                )
        effective: dict[int, dict[str, Any]] = {}
        for block_id in sorted(declared):
            block = dict(declared[block_id])
            old_block = old_blocks.get(block_id)
            if old_block is None:
                effective[block_id] = block
                continue
            if block["checksum"] == old_block["checksum"]:
                block["version"] = int(old_block["version"])
            else:
                if int(codebook["version"]) <= int(old["version"]):
                    raise PipelineError(
                        "E_SNAPSHOT_ROLLBACK",
                        f"Changed codebook block {block_id} requires a global version increase",
                        stage="codebook",
                        field="version",
                        details={"block_id": block_id},
                    )
                block["version"] = max(
                    int(block["version"]),
                    int(old_block["version"]) + 1,
                    int(codebook["version"]),
                )
            effective[block_id] = block
        summary = {
            "version": int(codebook["version"]),
            "checksum": codebook["checksum"],
            "dim": int(codebook["dim"]),
            "centroids_per_block": int(codebook["centroids_per_block"]),
            "blocks": [
                {
                    "block_id": block_id,
                    "version": int(effective[block_id]["version"]),
                    "checksum": effective[block_id]["checksum"],
                }
                for block_id in sorted(effective)
            ],
        }
        return effective, summary

    def _check_resource_estimates(
        self,
        previous_snapshot: dict[str, Any] | None,
    ) -> None:
        changed = self._input_changed_pairs(previous_snapshot)
        if len(changed) > self.limits["max_recompute_pairs"]:
            self._resource_or_degrade(
                "pair_recompute_pairs",
                "resources.max_recompute_pairs",
                len(changed),
                self.limits["max_recompute_pairs"],
            )
        estimate = codebook_memory_estimate(self.bundle.codebook)
        if estimate > self.limits["max_codebook_bytes"]:
            self._resource_or_degrade(
                "codebook_memory",
                "resources.max_codebook_bytes",
                estimate,
                self.limits["max_codebook_bytes"],
            )

    def _resource_or_degrade(
        self,
        category: str,
        field: str,
        observed: int,
        limit: int,
    ) -> None:
        if self.limits["on_limit"] == "degrade":
            self._degraded.append(
                {
                    "category": category,
                    "field": field,
                    "observed": int(observed),
                    "limit": int(limit),
                    "action": "streaming_or_chunked",
                }
            )
            return
        raise PipelineError(
            "E_RESOURCE_LIMIT",
            f"Resource limit exceeded for {category}",
            stage="resource",
            field=field,
            details={
                "category": category,
                "observed": int(observed),
                "limit": int(limit),
            },
        )

    def _input_changed_pairs(
        self,
        previous_snapshot: dict[str, Any] | None,
    ) -> set[str]:
        if previous_snapshot is None:
            return {sample["pair_id"] for sample in self.samples}
        old_samples = {
            sample["pair_id"]: sample for sample in previous_snapshot["samples"]
        }
        new_samples = self.samples_by_id
        changed = set(old_samples) ^ set(new_samples)
        for pair_id in set(old_samples) & set(new_samples):
            if sample_digest(old_samples[pair_id]) != sample_digest(new_samples[pair_id]):
                changed.add(pair_id)
        old_config = previous_snapshot["config"]
        new_config = config_semantic_view(self.bundle.config)
        if old_config != new_config:
            for section in ("image", "text", "align", "index", "retrieval", "graph"):
                if old_config.get(section) != new_config.get(section):
                    changed |= set(new_samples)
                    break
        if previous_snapshot["vocab"]["checksum"] != self.bundle.vocab["checksum"]:
            changed |= set(new_samples)
        if previous_snapshot["codebook"]["checksum"] != self.bundle.codebook["checksum"]:
            changed |= set(new_samples)
        return changed

    # --------------------------------------------------------------- encoders
    def _build_encoders(
        self,
        lineage: dict[str, Any],
    ) -> tuple[list[Artifact], list[Artifact]]:
        image_name = _type_node(self.graph, "image_encoder")
        text_name = _type_node(self.graph, "text_encoder")
        image_components: dict[str, dict[str, Any]] = {}
        text_components: dict[str, dict[str, Any]] = {}
        missing: list[tuple[str, dict[str, Any]]] = []
        for sample in self.samples:
            pair_id = sample["pair_id"]
            image_instance = f"{image_name}:{pair_id}"
            text_instance = f"{text_name}:{pair_id}"
            image_components[pair_id] = build_key_components(
                node_type="image_encoder",
                instance=image_instance,
                pair_id=pair_id,
                inputs=sample_image_fields(sample),
                upstream={},
                params={
                    "shape": [int(x) for x in self.bundle.config_info["shape"]],
                    "block_size": [
                        int(x) for x in self.bundle.config_info["block_size"]
                    ],
                    "projection_seed": self.bundle.config["image"]["projection_seed"],
                    "embed_dim": int(self.bundle.config_info["image_dim"]),
                    "encoder": "block_mean_fixed_projection_v1",
                },
            )
            text_components[pair_id] = build_key_components(
                node_type="text_encoder",
                instance=text_instance,
                pair_id=pair_id,
                inputs=sample_text_fields(sample),
                upstream={},
                params={
                    "dim": int(self.bundle.config_info["text_dim"]),
                    "pooling": "tf_weighted_mean_v1",
                },
                vocab={
                    "version": int(self.bundle.vocab["version"]),
                    "checksum": self.bundle.vocab["checksum"],
                },
            )
            if not self.cache.exists(key_from_components(image_components[pair_id])):
                missing.append(
                    ("image", {"sample": sample, "config": self.bundle.config})
                )
            if not self.cache.exists(key_from_components(text_components[pair_id])):
                missing.append(
                    (
                        "text",
                        {
                            "sample": sample,
                            "config": self.bundle.config,
                            "vocab": {
                                "version": self.bundle.vocab["version"],
                                "checksum": self.bundle.vocab["checksum"],
                                "languages": self.bundle.vocab["languages"],
                            },
                        },
                    )
                )
        results = self._run_encode_tasks(missing)
        image_artifacts: list[Artifact] = []
        text_artifacts: list[Artifact] = []
        for sample in self.samples:
            pair_id = sample["pair_id"]
            image_artifact = self._resolve(
                node_type="image_encoder",
                instance=f"{image_name}:{pair_id}",
                pair_id=pair_id,
                components=image_components[pair_id],
                compute=lambda sample=sample: encode_image(sample, self.bundle.config),
                precomputed=results.get(("image", pair_id)),
            )
            text_artifact = self._resolve(
                node_type="text_encoder",
                instance=f"{text_name}:{pair_id}",
                pair_id=pair_id,
                components=text_components[pair_id],
                compute=lambda sample=sample: encode_text(
                    sample, self.bundle.vocab, self.bundle.config
                ),
                precomputed=results.get(("text", pair_id)),
            )
            image_artifacts.append(image_artifact)
            text_artifacts.append(text_artifact)
        return image_artifacts, text_artifacts

    def _run_encode_tasks(
        self,
        missing: list[tuple[str, dict[str, Any]]],
    ) -> dict[tuple[str, str], dict[str, np.ndarray]]:
        if not missing:
            return {}
        if self.workers > 1 and len(missing) > 1:
            tasks = [
                (kind, payload)
                for kind, payload in missing
            ]
            with ProcessPoolExecutor(max_workers=min(self.workers, len(tasks))) as pool:
                outputs = list(pool.map(_worker_encode, tasks, chunksize=1))
            return {
                (kind, payload["sample"]["pair_id"]): output
                for (kind, payload), output in zip(tasks, outputs, strict=True)
            }
        return {
            (kind, payload["sample"]["pair_id"]): _worker_encode((kind, payload))
            for kind, payload in missing
        }

    # ------------------------------------------------------------------ align
    def _build_align(
        self,
        image_artifacts: list[Artifact],
        text_artifacts: list[Artifact],
        lineage: dict[str, Any],
    ) -> list[Artifact]:
        align_name = _type_node(self.graph, "align")
        image_by_pair = {artifact.pair_id: artifact for artifact in image_artifacts}
        text_by_pair = {artifact.pair_id: artifact for artifact in text_artifacts}
        artifacts: list[Artifact] = []
        for sample in self.samples:
            pair_id = sample["pair_id"]
            image = image_by_pair[pair_id]
            text = text_by_pair[pair_id]
            components = build_key_components(
                node_type="align",
                instance=f"{align_name}:{pair_id}",
                pair_id=pair_id,
                inputs={"pair_id": pair_id},
                upstream={
                    "image_encoder": image.product_hash,
                    "text_encoder": text.product_hash,
                },
                params={
                    "seed": self.bundle.config["align"]["seed"],
                    "dim": int(self.bundle.config_info["align_dim"]),
                    "scale": float(self.bundle.config["align"].get("scale", 1.0)),
                    "normalization": "l2",
                    "aligner": "fixed_dual_projection_v1",
                },
                vocab={
                    "version": int(self.bundle.vocab["version"]),
                    "checksum": self.bundle.vocab["checksum"],
                },
            )
            artifact = self._resolve(
                node_type="align",
                instance=f"{align_name}:{pair_id}",
                pair_id=pair_id,
                components=components,
                compute=lambda image=image, text=text, pair_id=pair_id: align_embeddings(
                    image.arrays["embedding"],
                    text.arrays["embedding"],
                    self.bundle.config,
                    pair_id=pair_id,
                ),
            )
            artifact.meta = dict(artifact.meta)
            artifact.meta["image_aligned_hash"] = array_digest(
                artifact.arrays["image_aligned"]
            )
            artifact.meta["text_aligned_hash"] = array_digest(
                artifact.arrays["text_aligned"]
            )
            artifact.meta["image_encoder_key"] = image.key
            artifact.meta["text_encoder_key"] = text.key
            artifacts.append(artifact)
        return artifacts

    # ------------------------------------------------------------------ index
    def _build_index(
        self,
        align_artifacts: list[Artifact],
        effective_blocks: dict[int, dict[str, Any]],
        effective_codebook: dict[str, Any],
        lineage: dict[str, Any],
    ) -> list[Artifact]:
        index_name = _type_node(self.graph, "index_build")
        num_shards = int(self.bundle.config_info["num_shards"])
        align_by_pair = {
            artifact.pair_id: artifact for artifact in align_artifacts
        }
        block_pairs: dict[int, list[str]] = {block_id: [] for block_id in effective_blocks}
        for sample in self.samples:
            block_id = _shard_for_pair(sample["pair_id"], num_shards)
            block_pairs[block_id].append(sample["pair_id"])
        artifacts: list[Artifact] = []
        for block_id in sorted(block_pairs):
            pair_ids = sorted(block_pairs[block_id])
            block = effective_blocks[block_id]
            components = build_key_components(
                node_type="index_build",
                instance=f"{index_name}:block-{block_id}",
                pair_id=None,
                inputs={
                    "block_id": block_id,
                    "pair_ids": pair_ids,
                },
                upstream={
                    "text_aligned": {
                        pair_id: array_digest(
                            align_by_pair[pair_id].arrays["text_aligned"]
                        )
                        for pair_id in pair_ids
                    }
                },
                params={
                    "num_shards": num_shards,
                    "quantization": "nearest_l2_with_residual_v1",
                    "vector_dim": int(self.bundle.config_info["align_dim"]),
                },
                vocab={
                    "version": int(self.bundle.vocab["version"]),
                    "checksum": self.bundle.vocab["checksum"],
                },
                codebook={
                    "block_id": block_id,
                    "version": int(block["version"]),
                    "checksum": block["checksum"],
                },
            )

            def compute(
                pair_ids=pair_ids,
                block=block,
            ) -> dict[str, np.ndarray]:
                vectors = np.stack(
                    [
                        align_by_pair[pair_id].arrays["text_aligned"]
                        for pair_id in pair_ids
                    ]
                ) if pair_ids else np.zeros(
                    (0, int(self.bundle.config_info["align_dim"])),
                    dtype=np.float64,
                )
                return quantize_vectors(vectors, block["centroids"])

            artifact = self._resolve(
                node_type="index_build",
                instance=f"{index_name}:block-{block_id}",
                pair_id=None,
                components=components,
                compute=compute,
                meta={"block_id": block_id, "pair_ids": pair_ids},
            )
            self._validate_index_artifact(
                artifact,
                pair_ids=pair_ids,
                align_by_pair=align_by_pair,
                block=block,
            )
            artifacts.append(artifact)
        return artifacts

    def _validate_index_artifact(
        self,
        artifact: Artifact,
        *,
        pair_ids: list[str],
        align_by_pair: dict[str, Artifact],
        block: dict[str, Any],
    ) -> None:
        block_id = int(artifact.meta["block_id"])
        version = int(block["version"])
        codes = artifact.arrays["codes"]
        residuals = artifact.arrays["residuals"]
        centroids = artifact.arrays["centroids"]
        expected_centroids = np.asarray(block["centroids"], dtype=np.float64)
        if not np.array_equal(centroids, expected_centroids):
            raise PipelineError(
                "E_INCONSISTENT",
                f"Index block {block_id} does not contain codebook version {version}",
                stage="index_build",
                field="centroids",
                details={
                    "block_id": block_id,
                    "codebook_version": version,
                    "category": "codebook_payload_mismatch",
                },
            )
        if not pair_ids:
            return
        vectors = np.stack(
            [align_by_pair[pair_id].arrays["text_aligned"] for pair_id in pair_ids]
        )
        expected = quantize_vectors(vectors, expected_centroids)
        for row, pair_id in enumerate(pair_ids):
            if int(codes[row]) != int(expected["codes"][row]):
                raise PipelineError(
                    "E_INCONSISTENT",
                    f"Index block {block_id} code mismatch for {pair_id}",
                    stage="index_build",
                    field="codes",
                    pair_id=pair_id,
                    details={
                        "block_id": block_id,
                        "codebook_version": version,
                        "category": "codeword_mismatch",
                    },
                )
            if not np.array_equal(residuals[row], expected["residuals"][row]):
                raise PipelineError(
                    "E_INCONSISTENT",
                    f"Index block {block_id} residual mismatch for {pair_id}",
                    stage="index_build",
                    field="residuals",
                    pair_id=pair_id,
                    details={
                        "block_id": block_id,
                        "codebook_version": version,
                        "category": "residual_mismatch",
                    },
                )
            reconstructed = centroids[int(codes[row])] + residuals[row]
            if not np.isfinite(reconstructed).all():
                raise PipelineError(
                    "E_INCONSISTENT",
                    f"Index block {block_id} reconstructs non-finite values",
                    stage="index_build",
                    field="reconstruction",
                    pair_id=pair_id,
                    details={
                        "block_id": block_id,
                        "codebook_version": version,
                        "category": "nonfinite_reconstruction",
                    },
                )

    # -------------------------------------------------------------- retrieval
    def _build_retrieval(
        self,
        align_artifacts: list[Artifact],
        index_artifacts: list[Artifact],
        effective_codebook: dict[str, Any],
        lineage: dict[str, Any],
        image_artifacts: list[Artifact],
        text_artifacts: list[Artifact],
    ) -> list[Artifact]:
        retrieve_name = _type_node(self.graph, "retrieve")
        image_name = _type_node(self.graph, "image_encoder")
        text_name = _type_node(self.graph, "text_encoder")
        align_name = _type_node(self.graph, "align")
        index_name = _type_node(self.graph, "index_build")
        align_by_pair = {artifact.pair_id: artifact for artifact in align_artifacts}
        image_by_pair = {artifact.pair_id: artifact for artifact in image_artifacts}
        text_by_pair = {artifact.pair_id: artifact for artifact in text_artifacts}
        index_by_block = {
            int(artifact.meta["block_id"]): artifact for artifact in index_artifacts
        }
        pair_block = {
            pair_id: _shard_for_pair(pair_id, int(self.bundle.config_info["num_shards"]))
            for pair_id in self.samples_by_id
        }
        target_mask_map = {
            pair_id: list(self.samples_by_id[pair_id]["modality_mask"])
            for pair_id in sorted(self.samples_by_id)
        }
        candidate_mask_digest = sha256_json(target_mask_map)
        index_upstream = {
            str(block_id): artifact.product_hash
            for block_id, artifact in sorted(index_by_block.items())
        }
        query_artifacts: list[Artifact] = []
        for sample in self.samples:
            query_pair = sample["pair_id"]
            query_align = align_by_pair[query_pair]
            query_image_hash = array_digest(query_align.arrays["image_aligned"])
            codebook_global = {
                "version": int(effective_codebook["version"]),
                "checksum": effective_codebook["checksum"],
                "blocks": [
                    {
                        "block_id": int(block["block_id"]),
                        "version": int(block["version"]),
                        "checksum": block["checksum"],
                    }
                    for block in sorted(
                        effective_codebook["blocks"],
                        key=lambda item: int(item["block_id"]),
                    )
                ],
            }
            components = build_key_components(
                node_type="retrieve",
                instance=f"{retrieve_name}:{query_pair}",
                pair_id=query_pair,
                inputs={
                    "query_modality_mask": sample["modality_mask"],
                    "candidate_mask_digest": candidate_mask_digest,
                },
                upstream={
                    "query_image_aligned": query_image_hash,
                    "index_blocks": index_upstream,
                },
                params={
                    "top_k": int(self.bundle.config_info["top_k"]),
                    "metric": "cosine",
                    "tie_break": "target_pair_id_asc",
                    "exclude_self": False,
                    "graph_digest": self.graph.digest(),
                },
                vocab={
                    "version": int(self.bundle.vocab["version"]),
                    "checksum": self.bundle.vocab["checksum"],
                },
                codebook=codebook_global,
            )
            retrieval_key = key_from_components(components)
            hit_lineage = {
                "vocab_version": int(self.bundle.vocab["version"]),
                "vocab_checksum": self.bundle.vocab["checksum"],
                "codebook_version": int(effective_codebook["version"]),
                "codebook_checksum": effective_codebook["checksum"],
                "graph_digest": self.graph.digest(),
                "query_vector_hash": query_image_hash,
                "index_block_hashes": dict(index_upstream),
                "candidate_mask_digest": candidate_mask_digest,
            }

            def compute(
                query_pair=query_pair,
                sample=sample,
                query_align=query_align,
                retrieval_key=retrieval_key,
                codebook_global=codebook_global,
            ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
                return self._compute_retrieval(
                    query_pair=query_pair,
                    query_mask=sample["modality_mask"],
                    query_align=query_align,
                    index_by_block=index_by_block,
                    pair_block=pair_block,
                    align_by_pair=align_by_pair,
                    image_by_pair=image_by_pair,
                    text_by_pair=text_by_pair,
                    image_name=image_name,
                    text_name=text_name,
                    align_name=align_name,
                    index_name=index_name,
                    retrieve_name=retrieve_name,
                    retrieval_key=retrieval_key,
                    codebook_global=codebook_global,
                hit_lineage=hit_lineage,
                )

            artifact = self._resolve(
                node_type="retrieve",
                instance=f"{retrieve_name}:{query_pair}",
                pair_id=query_pair,
                components=components,
                compute=compute,
            )
            query_artifacts.append(artifact)
        return query_artifacts

    def _validate_retrieval_artifacts(
        self,
        retrieval_artifacts: list[Artifact],
        align_artifacts: list[Artifact],
        index_artifacts: list[Artifact],
        effective_blocks: dict[int, dict[str, Any]],
    ) -> None:
        align_by_pair = {
            artifact.pair_id: artifact for artifact in align_artifacts
        }
        index_by_block = {
            int(artifact.meta["block_id"]): artifact for artifact in index_artifacts
        }
        pair_block = {
            pair_id: _shard_for_pair(
                pair_id, int(self.bundle.config_info["num_shards"])
            )
            for pair_id in self.samples_by_id
        }
        for artifact in sorted(
            retrieval_artifacts, key=lambda item: item.meta["query_pair_id"]
        ):
            query_pair = artifact.meta["query_pair_id"]
            query_mask = artifact.meta["query_modality_mask"]
            actual_hits = artifact.meta["hits"]
            if "image" not in query_mask:
                if actual_hits:
                    raise PipelineError(
                        "E_INCONSISTENT",
                        "Text-only query produced image-to-text hits",
                        stage="retrieve",
                        field="modality_mask",
                        pair_id=query_pair,
                        details={"category": "unexpected_hits"},
                    )
                continue
            candidates: list[str] = []
            vectors: list[np.ndarray] = []
            for target in sorted(self.samples_by_id):
                if "text" not in self.samples_by_id[target]["modality_mask"]:
                    continue
                block = index_by_block[pair_block[target]]
                row = list(block.meta["pair_ids"]).index(target)
                candidates.append(target)
                vectors.append(
                    block.arrays["centroids"][int(block.arrays["codes"][row])]
                    + block.arrays["residuals"][row]
                )
            if not candidates:
                if actual_hits:
                    raise PipelineError(
                        "E_INCONSISTENT",
                        "Query with no eligible targets produced hits",
                        stage="retrieve",
                        field="hits",
                        pair_id=query_pair,
                        details={"category": "unexpected_hits"},
                    )
                continue
            scores = cosine_scores(
                align_by_pair[query_pair].arrays["image_aligned"],
                np.stack(vectors),
            )
            ranked = sorted(
                zip(candidates, (float(value) for value in scores)),
                key=lambda item: (-item[1], item[0]),
            )
            top_k = min(int(self.bundle.config_info["top_k"]), len(ranked))
            expected = ranked[:top_k]
            if len(expected) != len(actual_hits):
                block_id = pair_block[expected[0][0]]
                raise PipelineError(
                    "E_INCONSISTENT",
                    "Top-k length cannot be reproduced from code and residual",
                    stage="retrieve",
                    field="hits",
                    pair_id=query_pair,
                    details={
                        "block_id": block_id,
                        "codebook_version": int(effective_blocks[block_id]["version"]),
                        "category": "topk_length",
                    },
                )
            for rank, ((target, score), hit) in enumerate(
                zip(expected, actual_hits, strict=True), start=1
            ):
                block_id = pair_block[target]
                if target != hit["target_pair_id"] or score != float(hit["score"]):
                    raise PipelineError(
                        "E_INCONSISTENT",
                        "Top-k cannot be reproduced from code and residual",
                        stage="retrieve",
                        field="score",
                        pair_id=query_pair,
                        details={
                            "block_id": block_id,
                            "codebook_version": int(
                                effective_blocks[block_id]["version"]
                            ),
                            "category": "topk_mismatch",
                            "rank": rank,
                            "expected_target": target,
                            "actual_target": hit["target_pair_id"],
                            "expected_score": score,
                            "actual_score": float(hit["score"]),
                        },
                    )

    def _compute_retrieval(
        self,
        *,
        query_pair: str,
        query_mask: list[str],
        query_align: Artifact,
        index_by_block: dict[int, Artifact],
        pair_block: dict[str, int],
        align_by_pair: dict[str, Artifact],
        image_by_pair: dict[str, Artifact],
        text_by_pair: dict[str, Artifact],
        image_name: str,
        text_name: str,
        align_name: str,
        index_name: str,
        retrieve_name: str,
        retrieval_key: str,
        codebook_global: dict[str, Any],
        hit_lineage: dict[str, Any],
    ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        scores_array = np.zeros(0, dtype=np.float64)
        if "image" in query_mask:
            candidates: list[str] = []
            target_vectors: list[np.ndarray] = []
            for target in sorted(self.samples_by_id):
                if "text" not in self.samples_by_id[target]["modality_mask"]:
                    continue
                block_id = pair_block[target]
                index_artifact = index_by_block[block_id]
                pair_ids = list(index_artifact.meta["pair_ids"])
                row = pair_ids.index(target)
                codes = index_artifact.arrays["codes"]
                residuals = index_artifact.arrays["residuals"]
                centroids = index_artifact.arrays["centroids"]
                reconstructed = centroids[int(codes[row])] + residuals[row]
                candidates.append(target)
                target_vectors.append(reconstructed)
            if candidates:
                matrix = np.stack(target_vectors)
                scores = cosine_scores(query_align.arrays["image_aligned"], matrix)
                ranked = sorted(
                    zip(candidates, (float(value) for value in scores)),
                    key=lambda item: (-item[1], item[0]),
                )
                top_k = min(int(self.bundle.config_info["top_k"]), len(ranked))
                scores_array = np.asarray(
                    [item[1] for item in ranked[:top_k]], dtype=np.float64
                )
                for rank, (target, score) in enumerate(ranked[:top_k], start=1):
                    block_id = pair_block[target]
                    index_artifact = index_by_block[block_id]
                    pair_ids = list(index_artifact.meta["pair_ids"])
                    row = pair_ids.index(target)
                    codebook_summary = {
                        "codebook_version": int(codebook_global["version"]),
                        "codebook_checksum": codebook_global["checksum"],
                        "block_id": block_id,
                        "block_version": int(
                            next(
                                block["version"]
                                for block in codebook_global["blocks"]
                                if int(block["block_id"]) == block_id
                            )
                        ),
                        "block_checksum": next(
                            block["checksum"]
                            for block in codebook_global["blocks"]
                            if int(block["block_id"]) == block_id
                        ),
                    }
                    target_align = align_by_pair[target]
                    residual_hash = array_digest(
                        index_artifact.arrays["residuals"][row]
                    )
                    reconstructed = (
                        index_artifact.arrays["centroids"][
                            int(index_artifact.arrays["codes"][row])
                        ]
                        + index_artifact.arrays["residuals"][row]
                    )
                    query_path = [
                        f"{image_name}:{query_pair}",
                        f"{align_name}:{query_pair}",
                        f"{retrieve_name}:{query_pair}",
                    ]
                    target_path = [
                        f"{text_name}:{target}",
                        f"{align_name}:{target}",
                        f"{index_name}:block-{block_id}",
                    ]
                    hits.append(
                        {
                            "query_pair_id": query_pair,
                            "rank": rank,
                            "target_pair_id": target,
                            "score": score,
                            "query_node_path": query_path,
                            "target_node_path": target_path,
                            "source_node_path": [
                                f"{image_name}:{query_pair}",
                                f"{text_name}:{target}",
                                f"{align_name}:{query_pair}",
                                f"{align_name}:{target}",
                                f"{index_name}:block-{block_id}",
                                f"{retrieve_name}:{query_pair}",
                            ],
                            "cache_keys": {
                                "image_encoder": image_by_pair[query_pair].key,
                                "query_align": align_by_pair[query_pair].key,
                                "text_encoder": text_by_pair[target].key,
                                "index_build": index_artifact.key,
                                "retrieve": retrieval_key,
                            },
                            "target_text_alignment_hash": array_digest(
                                target_align.arrays["text_aligned"]
                            ),
                            "vocab_version": int(self.bundle.vocab["version"]),
                            "vocab_checksum": self.bundle.vocab["checksum"],
                            **codebook_summary,
                            "query_vector_hash": array_digest(
                                query_align.arrays["image_aligned"]
                            ),
                            "target_code": int(
                                index_artifact.arrays["codes"][row]
                            ),
                            "target_residual_hash": residual_hash,
                            "target_reconstructed_hash": array_digest(reconstructed),
                            "lineage": hit_lineage,
                        }
                    )
        return (
            {"scores": scores_array},
            {
                "query_pair_id": query_pair,
                "query_modality_mask": list(query_mask),
                "hits": hits,
            },
        )

    # ---------------------------------------------------------------- result
    def _build_result_manifest(
        self,
        retrieval_artifacts: list[Artifact],
        lineage: dict[str, Any],
    ) -> tuple[Artifact, bytes]:
        queries = [
            {
                "query_pair_id": artifact.meta["query_pair_id"],
                "query_modality_mask": artifact.meta["query_modality_mask"],
                "hits": artifact.meta["hits"],
            }
            for artifact in sorted(
                retrieval_artifacts, key=lambda item: item.meta["query_pair_id"]
            )
        ]
        result_doc = {
            "schema": RESULT_SCHEMA,
            "lineage": lineage,
            "queries": queries,
        }
        result_bytes = pretty_json_bytes(result_doc)
        components = build_key_components(
            node_type="retrieval_manifest",
            instance="retrieval_manifest",
            pair_id=None,
            inputs={
                "query_pair_ids": [
                    artifact.meta["query_pair_id"] for artifact in retrieval_artifacts
                ],
                "result_schema": RESULT_SCHEMA,
                "snapshot_lineage": lineage,
            },
            upstream={
                artifact.instance: artifact.product_hash
                for artifact in retrieval_artifacts
            },
            params={
                "top_k": int(self.bundle.config_info["top_k"]),
                "ordering": "query_pair_id_asc",
            },
            vocab={
                "version": int(self.bundle.vocab["version"]),
                "checksum": self.bundle.vocab["checksum"],
            },
            codebook={
                "version": int(self.bundle.codebook["version"]),
                "checksum": self.bundle.codebook["checksum"],
            },
        )
        artifact = self._resolve(
            node_type="retrieval_manifest",
            instance="retrieval_manifest",
            pair_id=None,
            components=components,
            compute=lambda: ({}, result_doc),
            arrays_override={},
            track=False,
        )
        return artifact, result_bytes

    # -------------------------------------------------------------- machinery
    def _resolve(
        self,
        *,
        node_type: str,
        instance: str,
        pair_id: str | None,
        components: dict[str, Any],
        compute: Callable[[], Any],
        precomputed: dict[str, np.ndarray] | None = None,
        meta: dict[str, Any] | None = None,
        arrays_override: dict[str, np.ndarray] | None = None,
        track: bool = True,
    ) -> Artifact:
        key = key_from_components(components)
        if self.cache.exists(key):
            arrays, payload_meta, cache_meta = self.cache.get(key)
            artifact = Artifact(
                instance=instance,
                node_type=node_type,
                pair_id=pair_id,
                key=key,
                product_hash=cache_meta["product_hash"],
                arrays=arrays,
                meta=payload_meta,
            )
            if track:
                self._reused.append(self._node_report(artifact))
            return artifact
        try:
            if precomputed is not None:
                arrays = precomputed
                payload_meta = meta or {}
            else:
                computed = compute()
                if (
                    isinstance(computed, tuple)
                    and len(computed) == 2
                    and isinstance(computed[0], dict)
                ):
                    arrays, payload_meta = computed
                else:
                    arrays = computed
                    payload_meta = meta or {}
                if arrays_override is not None:
                    arrays = arrays_override
            cache_meta = self.cache.put(components, arrays, payload_meta)
        except PipelineError as exc:
            if exc.stage is None:
                exc.at_stage(node_type)
            if exc.pair_id is None and pair_id is not None:
                exc.pair_id = pair_id
            raise
        artifact = Artifact(
            instance=instance,
            node_type=node_type,
            pair_id=pair_id,
            key=key,
            product_hash=cache_meta["product_hash"],
            arrays=arrays,
            meta=payload_meta,
        )
        if track:
            self._recomputed.append(self._node_report(artifact))
        return artifact

    @staticmethod
    def _node_report(artifact: Artifact) -> dict[str, Any]:
        return {
            "instance": artifact.instance,
            "node_type": artifact.node_type,
            "pair_id": artifact.pair_id,
            "cache_key": artifact.key,
            "product_hash": artifact.product_hash,
        }

    def _affected_pairs(
        self,
        previous_snapshot: dict[str, Any] | None,
        recomputed_pairs: list[str],
        recomputed_blocks: list[int],
        index_blocks: dict[str, dict[str, Any]],
    ) -> list[str]:
        affected = set(recomputed_pairs)
        affected |= self._input_changed_pairs(previous_snapshot)
        for block_id in recomputed_blocks:
            entry = index_blocks.get(str(block_id))
            if entry:
                affected |= set(entry["pair_ids"])
        if previous_snapshot is not None:
            old_pairs = {sample["pair_id"] for sample in previous_snapshot["samples"]}
            affected |= old_pairs - set(self.samples_by_id)
        return sorted(affected)

