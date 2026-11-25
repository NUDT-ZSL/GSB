"""Deterministic factories shared by the bundled pytest suite."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .ingest import content_hash_for


def make_record(
    doc_id: str,
    text: str | None = None,
    *,
    shard_id: str = "shard-000",
    lang: str = "en",
    source: str = "web",
    quality_score: float = 0.5,
    token_estimate: int = 100,
    content_hash: str | None = None,
) -> dict[str, Any]:
    if text is None:
        text = ("deterministic signature block content for " + doc_id + " ") * 3
    return {
        "shard_id": shard_id,
        "doc_id": doc_id,
        "text": text,
        "lang": lang,
        "source": source,
        "token_estimate": token_estimate,
        "quality_score": quality_score,
        "content_hash": content_hash or content_hash_for(text),
    }


def base_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "config_version": 1,
        "chunk_signature_length": 16,
        "similarity_threshold": 0.75,
        "candidate_window": 3,
        "validation_mode": "full",
        "sampling_seed": 20261126,
        "bucket_strategy": "block_digest",
        "allowed_langs": ["zh", "en", "ja", "code", "multi"],
        "limits": {
            "max_shards": 16,
            "max_blocks_per_doc": 128,
            "max_cache_memory_bytes": 2 * 1024 * 1024,
            "latency_budget_ms": 50.0,
            "query_loop_iterations": 7,
            "batch_size": 3,
            "cache_overflow_policy": "evict_then_spill",
        },
        "latency_model": {"base_ms": 0.01, "per_block_ms": 0.001, "per_comparison_ms": 0.002},
    }
    for key, value in overrides.items():
        if key == "limits" and isinstance(value, dict):
            config["limits"].update(value)
        else:
            config[key] = value
    return config


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    return path


def write_config(path: Path, config: dict[str, Any]) -> Path:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
