from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import canonical_json_bytes, sha256_json
from .errors import PipelineError
from .graph import CANONICAL_NODE_TYPES, GraphSpec


def _read_json(path: str | Path, stage: str) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"File not found: {path}",
            stage=stage,
            field="path",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"Cannot read JSON file {path}: {exc}",
            stage=stage,
            field="path",
        ) from exc
    if not isinstance(value, dict):
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"JSON root must be an object: {path}",
            stage=stage,
            field="root",
        )
    return value


def _positive_int(value: Any, field: str, stage: str = "config") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"{field} must be a positive integer",
            stage=stage,
            field=field,
        )
    return value


def _nonnegative_int(value: Any, field: str, stage: str = "config") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"{field} must be a non-negative integer",
            stage=stage,
            field=field,
        )
    return value


def _finite_number(value: Any, field: str, stage: str = "config") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"{field} must be a finite number",
            stage=stage,
            field=field,
        )
    result = float(value)
    if not math.isfinite(result):
        raise PipelineError(
            "E_CONFIG_INVALID",
            f"{field} must be finite",
            stage=stage,
            field=field,
        )
    return result


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    image = config.get("image")
    text = config.get("text")
    align = config.get("align")
    index = config.get("index")
    retrieval = config.get("retrieval")
    if not all(isinstance(x, dict) for x in (image, text, align, index, retrieval)):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image, text, align, index and retrieval must be objects",
            stage="config",
            field="config",
        )
    shape = image.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != 3
        or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in shape)
        or shape[2] != 3
    ):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image.shape must be [height, width, 3] with positive integers",
            stage="config",
            field="image.shape",
        )
    block_size = image.get("block_size")
    if (
        not isinstance(block_size, list)
        or len(block_size) != 2
        or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in block_size)
    ):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image.block_size must contain two positive integers",
            stage="config",
            field="image.block_size",
        )
    if shape[0] % block_size[0] or shape[1] % block_size[1]:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image.shape must be divisible by image.block_size",
            stage="config",
            field="image.block_size",
        )
    image_dim = _positive_int(image.get("embed_dim"), "image.embed_dim")
    text_dim = _positive_int(text.get("embed_dim"), "text.embed_dim")
    if image_dim != text_dim:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image.embed_dim and text.embed_dim must match",
            stage="config",
            field="text.embed_dim",
        )
    image.get("projection_seed")
    if not isinstance(image.get("projection_seed"), (int, str)) or isinstance(
        image.get("projection_seed"), bool
    ):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "image.projection_seed must be an integer or string",
            stage="config",
            field="image.projection_seed",
        )
    align_dim = _positive_int(align.get("dim"), "align.dim")
    if not isinstance(align.get("seed"), (int, str)) or isinstance(align.get("seed"), bool):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "align.seed must be an integer or string",
            stage="config",
            field="align.seed",
        )
    _finite_number(align.get("scale", 1.0), "align.scale")
    num_shards = _positive_int(index.get("num_shards"), "index.num_shards")
    _positive_int(retrieval.get("top_k"), "retrieval.top_k")
    metric = retrieval.get("metric", "cosine")
    if metric != "cosine":
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Only cosine retrieval is supported",
            stage="config",
            field="retrieval.metric",
        )
    resources = config.get("resources", {})
    if not isinstance(resources, dict):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "resources must be an object",
            stage="config",
            field="resources",
        )
    for field in (
        "max_recompute_pairs",
        "max_codebook_bytes",
        "max_cache_entries",
    ):
        if field in resources:
            _positive_int(resources[field], f"resources.{field}")
    policy = resources.get("on_limit", "error")
    if policy not in {"error", "degrade"}:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "resources.on_limit must be 'error' or 'degrade'",
            stage="config",
            field="resources.on_limit",
        )
    graph = GraphSpec.from_config(config)
    _validate_graph_shape(graph)
    if len(graph.nodes) < len(CANONICAL_NODE_TYPES):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Graph must contain all five node types",
            stage="graph",
            field="graph.nodes",
        )
    return {
        "image_dim": image_dim,
        "text_dim": text_dim,
        "align_dim": align_dim,
        "num_shards": num_shards,
        "top_k": int(retrieval["top_k"]),
        "shape": tuple(int(x) for x in shape),
        "block_size": tuple(int(x) for x in block_size),
        "graph": graph,
    }


def _reachable(edges: Iterable[tuple[str, str]], start: str, target: str) -> bool:
    neighbors: dict[str, list[str]] = {}
    for source, dest in edges:
        neighbors.setdefault(source, []).append(dest)
    stack = [start]
    seen: set[str] = set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(neighbors.get(node, []))
    return False


def _validate_graph_shape(graph: GraphSpec) -> None:
    by_type: dict[str, list[str]] = {}
    for name, node in graph.nodes.items():
        by_type.setdefault(node["type"], []).append(name)
    for node_type in CANONICAL_NODE_TYPES:
        if len(by_type.get(node_type, [])) != 1:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Graph must contain exactly one {node_type} node",
                stage="graph",
                field="graph.nodes",
            )
    image = by_type["image_encoder"][0]
    text = by_type["text_encoder"][0]
    align = by_type["align"][0]
    index = by_type["index_build"][0]
    retrieve = by_type["retrieve"][0]
    required = [
        (image, align),
        (text, align),
        (align, index),
        (align, retrieve),
        (index, retrieve),
    ]
    for source, target in required:
        if source != target and not _reachable(graph.edges, source, target):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Graph requires a path {source} -> {target}",
                stage="graph",
                field="graph.edges",
            )
    if graph.entrypoint != retrieve:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Graph entrypoint must be the retrieve node",
            stage="graph",
            field="graph.entrypoint",
        )


def load_config(path: str | Path) -> dict[str, Any]:
    config = _read_json(path, "config")
    validate_config(config)
    return config


def config_semantic_view(config: dict[str, Any]) -> dict[str, Any]:
    """Remove operational knobs that must not invalidate semantic artifacts."""
    return {
        key: value
        for key, value in config.items()
        if key not in {"resources"}
    }


def config_digest(config: dict[str, Any]) -> str:
    return sha256_json(config_semantic_view(config))


def load_vocab(path: str | Path) -> dict[str, Any]:
    vocab = _read_json(path, "vocab")
    if vocab.get("schema") != "cm-vocab-v1":
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Unsupported vocab schema",
            stage="vocab",
            field="schema",
        )
    _positive_int(vocab.get("version"), "version", "vocab")
    languages = vocab.get("languages")
    if not isinstance(languages, dict) or not languages:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "vocab.languages must be a non-empty object",
            stage="vocab",
            field="languages",
        )
    for lang in sorted(languages):
        entry = languages[lang]
        if not isinstance(entry, dict) or not isinstance(entry.get("weights"), dict):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"vocab language {lang} must contain a weights object",
                stage="vocab",
                field=f"languages.{lang}.weights",
            )
        if not entry["weights"]:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"vocab language {lang} has no tokens",
                stage="vocab",
                field=f"languages.{lang}.weights",
            )
        for token in sorted(entry["weights"]):
            if not isinstance(token, str) or not token:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    "Vocabulary token must be a non-empty string",
                    stage="vocab",
                    field=f"languages.{lang}.weights",
                )
            weight = entry["weights"][token]
            _finite_number(weight, f"languages.{lang}.weights.{token}", "vocab")
            if float(weight) <= 0.0:
                raise PipelineError(
                    "E_CONFIG_INVALID",
                    "Vocabulary token weight must be positive",
                    stage="vocab",
                    field=f"languages.{lang}.weights.{token}",
                )
    actual = vocab.get("checksum")
    expected = sha256_json(
        {
            "schema": vocab["schema"],
            "languages": vocab["languages"],
        }
    )
    if actual != expected:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Vocabulary checksum does not match its content",
            stage="vocab",
            field="checksum",
            details={"expected": expected, "actual": actual},
        )
    return vocab


def load_codebook(path: str | Path) -> dict[str, Any]:
    codebook = _read_json(path, "codebook")
    if codebook.get("schema") != "cm-codebook-v1":
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Unsupported codebook schema",
            stage="codebook",
            field="schema",
        )
    _positive_int(codebook.get("version"), "version", "codebook")
    dim = _positive_int(codebook.get("dim"), "dim", "codebook")
    centroids_per_block = _positive_int(
        codebook.get("centroids_per_block"),
        "centroids_per_block",
        "codebook",
    )
    blocks = codebook.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "codebook.blocks must be a non-empty list",
            stage="codebook",
            field="blocks",
        )
    seen: set[int] = set()
    normalized_blocks: list[dict[str, Any]] = []
    for position, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise PipelineError(
                "E_CONFIG_INVALID",
                "Each codebook block must be an object",
                stage="codebook",
                field=f"blocks[{position}]",
            )
        block_id = block.get("block_id")
        if isinstance(block_id, bool) or not isinstance(block_id, int) or block_id < 0:
            raise PipelineError(
                "E_CONFIG_INVALID",
                "Codebook block_id must be a non-negative integer",
                stage="codebook",
                field=f"blocks[{position}].block_id",
            )
        if block_id in seen:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Duplicate codebook block_id: {block_id}",
                stage="codebook",
                field="blocks",
            )
        seen.add(block_id)
        _positive_int(block.get("version"), f"blocks[{position}].version", "codebook")
        try:
            centroids = np.asarray(block.get("centroids"), dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Invalid centroids for codebook block {block_id}",
                stage="codebook",
                field=f"blocks[{position}].centroids",
            ) from exc
        if centroids.shape != (centroids_per_block, dim):
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Codebook block {block_id} centroids must have shape "
                f"({centroids_per_block}, {dim})",
                stage="codebook",
                field=f"blocks[{position}].centroids",
            )
        if not np.isfinite(centroids).all():
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Codebook block {block_id} contains non-finite values",
                stage="codebook",
                field=f"blocks[{position}].centroids",
            )
        block_checksum = sha256_json(
            {
                "block_id": block_id,
                "centroids": np.ascontiguousarray(centroids).tolist(),
            }
        )
        if block.get("checksum") != block_checksum:
            raise PipelineError(
                "E_CONFIG_INVALID",
                f"Codebook block {block_id} checksum mismatch",
                stage="codebook",
                field=f"blocks[{position}].checksum",
                details={
                    "expected": block_checksum,
                    "actual": block.get("checksum"),
                    "block_id": block_id,
                },
            )
        normalized = dict(block)
        normalized["centroids"] = centroids
        normalized["checksum"] = block_checksum
        normalized_blocks.append(normalized)
    normalized_blocks.sort(key=lambda item: item["block_id"])
    expected = _codebook_checksum(
        schema=codebook["schema"],
        dim=dim,
        centroids_per_block=centroids_per_block,
        blocks=normalized_blocks,
    )
    if codebook.get("checksum") != expected:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Codebook checksum does not match its content",
            stage="codebook",
            field="checksum",
            details={"expected": expected, "actual": codebook.get("checksum")},
        )
    result = dict(codebook)
    result["blocks"] = normalized_blocks
    return result


def _codebook_checksum(
    *,
    schema: str,
    dim: int,
    centroids_per_block: int,
    blocks: list[dict[str, Any]],
) -> str:
    return sha256_json(
        {
            "schema": schema,
            "dim": dim,
            "centroids_per_block": centroids_per_block,
            "blocks": [
                {
                    "block_id": block["block_id"],
                    "checksum": block["checksum"],
                }
                for block in sorted(blocks, key=lambda item: item["block_id"])
            ],
        }
    )


def codebook_summary(codebook: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": codebook["version"],
        "checksum": codebook["checksum"],
        "dim": codebook["dim"],
        "centroids_per_block": codebook["centroids_per_block"],
        "blocks": [
            {
                "block_id": block["block_id"],
                "version": block["version"],
                "checksum": block["checksum"],
            }
            for block in sorted(codebook["blocks"], key=lambda item: item["block_id"])
        ],
    }


def codebook_blocks_by_id(codebook: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(block["block_id"]): block for block in codebook["blocks"]}


def validate_codebook_against_config(
    codebook: dict[str, Any],
    config_info: dict[str, Any],
) -> None:
    if int(codebook["dim"]) != int(config_info["align_dim"]):
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Codebook dim must equal align.dim",
            stage="codebook",
            field="dim",
            details={
                "codebook_dim": int(codebook["dim"]),
                "align_dim": int(config_info["align_dim"]),
            },
        )
    block_ids = sorted(int(block["block_id"]) for block in codebook["blocks"])
    expected = list(range(int(config_info["num_shards"])))
    if block_ids != expected:
        raise PipelineError(
            "E_CONFIG_INVALID",
            "Codebook block ids must equal 0..num_shards-1",
            stage="codebook",
            field="blocks",
            details={"expected": expected, "actual": block_ids},
        )


def sample_image_fields(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "image_seed": sample["image_seed"],
        "image_shape": sample["image_shape"],
    }


def sample_text_fields(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "text": sample["text"],
        "text_lang": sample["text_lang"],
        "caption_revision": sample["caption_revision"],
    }


def sample_digest(sample: dict[str, Any]) -> str:
    return sha256_json(sample)


def samples_digest(samples: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(samples, key=lambda item: item["pair_id"])
    return sha256_json(
        [
            {
                "pair_id": sample["pair_id"],
                "digest": sample_digest(sample),
            }
            for sample in ordered
        ]
    )


def resource_limits(config: dict[str, Any]) -> dict[str, Any]:
    resources = config.get("resources", {})
    return {
        "max_recompute_pairs": int(resources.get("max_recompute_pairs", 1_000_000)),
        "max_codebook_bytes": int(
            resources.get("max_codebook_bytes", 512 * 1024 * 1024)
        ),
        "max_cache_entries": int(resources.get("max_cache_entries", 1_000_000)),
        "on_limit": resources.get("on_limit", "error"),
    }


def codebook_memory_estimate(codebook: dict[str, Any]) -> int:
    return sum(
        int(np.asarray(block["centroids"], dtype=np.float64).nbytes)
        for block in codebook["blocks"]
    )

