"""Deterministic block signatures, document digest, and bucket assignment."""
from __future__ import annotations

from typing import Any

from .canonical import hash_json, sha256_text
from .config import stage_parameters
from .errors import E_RESOURCE_LIMIT, WorkbenchError
from .ingest import normalize_text


def signature_cache_inputs(document: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    text_hash = document.get("text_hash") or document.get("content_hash")
    if not text_hash:
        text_hash = sha256_text(normalize_text(str(document["text"])))
    return {
        "doc_id": document["doc_id"],
        "text_hash": text_hash,
        "signature_parameters": stage_parameters(config, "signature"),
    }


def build_signature(document: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    chunk_length = int(config["chunk_signature_length"])
    text = document.get("normalized_text")
    if text is None:
        text = normalize_text(str(document["text"]))
    blocks: list[dict[str, Any]] = []

    for position, start in enumerate(range(0, len(text), chunk_length)):
        chunk = text[start : start + chunk_length]
        blocks.append(
            {
                "position": position,
                "start": start,
                "end": start + len(chunk),
                "length": len(chunk),
                "signature": sha256_text(chunk),
            }
        )

    max_blocks = int(config["limits"]["max_blocks_per_doc"])
    if len(blocks) > max_blocks:
        raise WorkbenchError(
            E_RESOURCE_LIMIT,
            "Document produces more signature blocks than max_blocks_per_doc",
            field="limits.max_blocks_per_doc",
            details={"doc_id": document["doc_id"], "actual_blocks": len(blocks), "limit": max_blocks},
        )

    ordered_signatures = [block["signature"] for block in blocks]
    doc_digest = hash_json({"ordered_block_signatures": ordered_signatures})
    unique_signatures = sorted(set(ordered_signatures))
    text_hash = document.get("text_hash") or document.get("content_hash") or sha256_text(text)

    if config["bucket_strategy"] == "block_digest":
        bucket_keys = [f"block:{signature}" for signature in unique_signatures]
        bucket_digests = list(unique_signatures)
    elif config["bucket_strategy"] == "anchor_digest":
        anchor_count = min(int(config["bucket_anchor_count"]), len(unique_signatures))
        bucket_digests = unique_signatures[:anchor_count]
        bucket_keys = [f"doc_anchor:{signature}" for signature in bucket_digests]
    else:
        prefix = int(config["bucket_prefix_chars"])
        bucket_keys = [f"doc_digest:{doc_digest[:prefix]}"]
        bucket_digests = [doc_digest]

    return {
        "doc_id": document["doc_id"],
        "shard_id": document["shard_id"],
        "content_hash": document["content_hash"],
        "text_hash": text_hash,
        "chunk_signature_length": chunk_length,
        "block_count": len(blocks),
        "blocks": blocks,
        "doc_digest": doc_digest,
        "bucket_strategy": config["bucket_strategy"],
        "document_bucket_digests": bucket_digests,
        "bucket_keys": bucket_keys,
    }
