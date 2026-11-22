from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .errors import (
    E_DOC_TOO_SHORT,
    E_SIG_LEN_INVALID,
    WorkbenchError,
)
from .util import content_hash, sha256_text

TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
SENTINEL = np.uint64(0xFFFFFFFFFFFFFFFF)


def normalize_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return TOKEN_PATTERN.findall(normalized)


def block_signature(block_text: str, signature_length: int) -> str:
    payload = f"cdw-block-v1|{signature_length}|{block_text}".encode()
    return hashlib.blake2b(
        payload,
        digest_size=8,
        person=b"cdw-block",
    ).hexdigest()


def _summary_values(signatures: list[str], summary_size: int) -> tuple[list[int], list[int]]:
    values = np.asarray([int(signature, 16) for signature in signatures], dtype=np.uint64)
    sorted_values = np.sort(values, kind="stable")
    if sorted_values.size >= summary_size:
        selected = sorted_values[:summary_size]
        selected_positions = [
            int(np.flatnonzero(values == value)[0]) for value in selected
        ]
    else:
        padding = np.full(summary_size - sorted_values.size, SENTINEL, dtype=np.uint64)
        selected = np.concatenate([sorted_values, padding])
        selected_positions = [
            int(np.flatnonzero(values == value)[0]) for value in sorted_values
        ]
    return [int(value) for value in selected], selected_positions


def build_signature_record(
    doc: dict[str, Any], config: WorkbenchConfig
) -> dict[str, Any]:
    if not isinstance(config.signature_length, int) or config.signature_length <= 0:
        raise WorkbenchError(
            E_SIG_LEN_INVALID,
            "分块签名长度必须为正整数",
            stage="signature_generation",
            details={"doc_id": doc.get("doc_id"), "signature_length": config.signature_length},
            completed_stages=[{"name": "ingest_validate", "status": "completed"}],
        )

    text = str(doc.get("text", ""))
    tokens = normalize_tokens(text)
    if len(tokens) < config.signature_length:
        raise WorkbenchError(
            E_DOC_TOO_SHORT,
            "文档长度不足以形成一个签名块",
            stage="signature_generation",
            details={
                "doc_id": doc.get("doc_id"),
                "normalized_token_count": len(tokens),
                "signature_length": config.signature_length,
            },
            completed_stages=[{"name": "ingest_validate", "status": "completed"}],
        )

    blocks: list[dict[str, Any]] = []
    for position, start in enumerate(range(0, len(tokens), config.signature_length)):
        block_tokens = tokens[start : start + config.signature_length]
        block_text = " ".join(block_tokens)
        signature = block_signature(block_text, config.signature_length)
        blocks.append(
            {
                "position": position,
                "token_start": start,
                "token_count": len(block_tokens),
                "text_sha256": sha256_text(block_text),
                "signature": signature,
            }
        )

    signatures = [block["signature"] for block in blocks]
    summary_values, anchor_positions = _summary_values(signatures, config.summary_size)
    bands: list[str] = []
    for band_index in range(config.summary_bands):
        start = band_index * config.values_per_band
        end = start + config.values_per_band
        bands.append("".join(f"{value:016x}" for value in summary_values[start:end]))

    summary_key = "".join(bands)
    bucket_key = f"band0:{bands[0]}"
    normalized_text = " ".join(tokens)
    return {
        "doc_id": doc["doc_id"],
        "shard_id": doc["shard_id"],
        "normalization_version": config.normalization_version,
        "normalized_text_sha256": sha256_text(normalized_text),
        "normalized_token_count": len(tokens),
        "signature_length": config.signature_length,
        "blocks": blocks,
        "block_signatures": signatures,
        "summary": {
            "key": summary_key,
            "bands": bands,
            "values": [f"{value:016x}" for value in summary_values],
            "anchor_positions": anchor_positions,
            "block_count": len(blocks),
            "summary_size": config.summary_size,
        },
        "bucket_key": bucket_key,
        "content_hash": content_hash(
            {
                "doc_id": doc["doc_id"],
                "shard_id": doc["shard_id"],
                "text": text,
                "lang": doc.get("lang"),
                "source": doc.get("source"),
                "token_estimate": doc.get("token_estimate"),
                "quality_score": doc.get("quality_score"),
            }
        ),
    }

