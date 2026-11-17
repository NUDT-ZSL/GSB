from __future__ import annotations

import re
import unicodedata
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import numpy as np

from .config import feature_projection
from .io_utils import sha256_obj, stable_task_key


def normalize_text(text: str, config: dict[str, Any]) -> str:
    if not config["normalize_text"]:
        return text.strip()
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()
    normalized = " ".join(normalized.split())
    return normalized.strip()


def tokenize_text(text: str, config: dict[str, Any]) -> list[str]:
    normalized = normalize_text(text, config)
    pattern = re.compile(config["subword_pattern"])
    return [match.group(0) for match in pattern.finditer(normalized)]


def fixed_hash_bucket(token: str, config: dict[str, Any]) -> int:
    digest = hashlib.blake2b(
        token.encode("utf-8"), digest_size=8, person=b"rewarddiag-v1"
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=False) % config["hash_buckets"]


def subword_rule_version(config: dict[str, Any]) -> str:
    projection = feature_projection(config)
    return sha256_obj(projection)[:20]


def build_count_vector(tokens: list[str], config: dict[str, Any]) -> tuple[list[int], list[int]]:
    counts: dict[int, int] = {}
    hash_buckets: set[int] = set()
    for token in tokens:
        bucket = fixed_hash_bucket(token, config)
        hash_buckets.add(bucket)
        index = bucket % config["feature_dimensions"]
        counts[index] = counts.get(index, 0) + 1
    vector = np.zeros(config["feature_dimensions"], dtype=np.int64)
    for index in sorted(counts):
        vector[index] = counts[index]
    return vector.tolist(), sorted(hash_buckets)


def extract_pair(pair: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    pair_id = pair["pair_id"]
    normalized: dict[str, str] = {}
    text_fingerprints: dict[str, str] = {}
    vectors: dict[str, list[int]] = {}
    hash_bucket_refs: set[int] = set()
    token_counts: dict[str, int] = {}
    empty_subword_sets: list[str] = []

    for field in ("prompt", "chosen", "rejected"):
        text = normalize_text(pair[field], config)
        tokens = tokenize_text(pair[field], config)
        normalized[field] = text
        text_fingerprints[field] = sha256_obj(text)
        vector, buckets = build_count_vector(tokens, config)
        vectors[field] = vector
        hash_bucket_refs.update(buckets)
        token_counts[field] = len(tokens)
        if not tokens:
            empty_subword_sets.append(field)

    referenced_buckets = sorted(
        {
            index
            for vector in vectors.values()
            for index, count in enumerate(vector)
            if count
        }
    )
    content_fingerprint = sha256_obj(normalized)
    cache_key = sha256_obj(
        {
            "content_fingerprint": content_fingerprint,
            "feature_projection": feature_projection(config),
        }
    )
    warnings: list[dict[str, Any]] = []
    if empty_subword_sets:
        warnings.append(
            {
                "code": "W_EMPTY_SUBWORDS",
                "fields": sorted(empty_subword_sets),
            }
        )

    return {
        "pair_id": pair_id,
        "cache_key": cache_key,
        "content_fingerprint": content_fingerprint,
        "subword_rule_version": subword_rule_version(config),
        "normalized": normalized,
        "text_fingerprints": text_fingerprints,
        "vectors": vectors,
        "token_counts": token_counts,
        "hash_bucket_refs": sorted(hash_bucket_refs),
        "referenced_buckets": referenced_buckets,
        "empty_subword_sets": sorted(empty_subword_sets),
        "warnings": warnings,
        "task_sort_key": stable_task_key(pair_id, "features"),
    }


def _feature_failure(
    pair_id: str, batch_id: str, stage: str, code: str, exc: BaseException
) -> dict[str, Any]:
    return {
        "pair_id": pair_id,
        "batch_id": batch_id,
        "stage": stage,
        "business_code": code,
        "exception_type": type(exc).__name__,
        "reason": str(exc),
        "source": None,
    }


def extract_many(
    pairs: dict[str, dict[str, Any]],
    config: dict[str, Any],
    batch_id: str,
    fault_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    fault_map = fault_map or {}
    results: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    task_keys: dict[str, str] = {
        pair_id: stable_task_key(pair_id, "features") for pair_id in pairs
    }

    def worker(pair_id: str) -> tuple[str, dict[str, Any] | None, BaseException | None]:
        try:
            if fault_map.get(pair_id) == "features":
                raise RuntimeError("injected feature-stage exception")
            return pair_id, extract_pair(pairs[pair_id], config), None
        except BaseException as exc:
            return pair_id, None, exc

    ordered_ids = sorted(pairs)
    if config["concurrency"] == 1 or len(ordered_ids) <= 1:
        completed = [worker(pair_id) for pair_id in ordered_ids]
    else:
        completed = []
        with ThreadPoolExecutor(max_workers=config["concurrency"]) as executor:
            futures = {
                executor.submit(worker, pair_id): pair_id for pair_id in ordered_ids
            }
            for future in as_completed(futures):
                completed.append(future.result())

    for pair_id, result, error in sorted(completed, key=lambda item: item[0]):
        if error is not None:
            failures.append(
                _feature_failure(
                    pair_id,
                    batch_id,
                    "features",
                    "E_FEATURE_EXTRACTION_ERROR",
                    error,
                )
            )
        elif result is not None:
            results[pair_id] = result

    bucket_references: dict[str, list[str]] = {}
    for pair_id in sorted(results):
        for bucket in results[pair_id]["referenced_buckets"]:
            bucket_references.setdefault(str(bucket), []).append(pair_id)
    for bucket in bucket_references:
        bucket_references[bucket].sort()

    return {
        "entries": dict(sorted(results.items())),
        "failures": sorted(failures, key=lambda item: (item["pair_id"], item["stage"])),
        "bucket_references": dict(sorted(bucket_references.items(), key=lambda item: int(item[0]))),
        "task_keys": dict(sorted(task_keys.items())),
    }
