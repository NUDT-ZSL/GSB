"""Algorithm whitelist, digest validation and budgeted real hash work."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Callable

from .codes import HASH_WHITELIST_VERSION, E_HASH_ALGO, E_HASH_WORK_LIMIT, E_TIMEOUT
from .model import AuditError


ALGORITHM_WHITELIST: dict[str, dict[str, object]] = {
    "sha256": {"bits": 256, "hex_length": 64, "kind": "content"},
    "hmac-sha256": {"bits": 256, "hex_length": 64, "kind": "keyed-content"},
}


@dataclass(frozen=True)
class DigestSpec:
    algorithm: str
    digest: str
    bits: int


def normalize_algorithm(algorithm: object) -> str:
    if not isinstance(algorithm, str):
        raise AuditError(E_HASH_ALGO, "digest algorithm must be a string", "algorithm")
    normalized = algorithm.strip().lower().replace("_", "-")
    aliases = {"hmacsha256": "hmac-sha256", "sha-256": "sha256"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in ALGORITHM_WHITELIST:
        raise AuditError(
            E_HASH_ALGO,
            f"algorithm {algorithm!r} is not in {HASH_WHITELIST_VERSION}",
            "algorithm",
        )
    return normalized


def validate_digest(algorithm: object, digest: object) -> DigestSpec:
    algo = normalize_algorithm(algorithm)
    if not isinstance(digest, str) or not digest:
        raise AuditError(E_HASH_ALGO, "digest must be a non-empty hexadecimal string", "digest")
    value = digest.strip().lower()
    if ":" in value:
        prefix, value = value.split(":", 1)
        prefix = normalize_algorithm(prefix)
        if prefix != algo:
            raise AuditError(E_HASH_ALGO, "digest prefix does not match declared algorithm", "digest")
    expected_length = int(ALGORITHM_WHITELIST[algo]["hex_length"])
    if len(value) != expected_length:
        raise AuditError(
            E_HASH_ALGO,
            f"{algo} requires {expected_length} hex chars, got {len(value)}",
            "digest",
        )
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise AuditError(E_HASH_ALGO, "digest is not valid hexadecimal", "digest") from exc
    actual_bits = len(raw) * 8
    expected_bits = int(ALGORITHM_WHITELIST[algo]["bits"])
    if actual_bits != expected_bits:
        raise AuditError(
            E_HASH_ALGO,
            f"digest width {actual_bits} does not match declared {expected_bits}",
            "digest",
        )
    return DigestSpec(algo, value, expected_bits)


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.strip().lower(), right.strip().lower())


def hash_payload(
    algorithm: str,
    payload: bytes,
    *,
    key: bytes | None = None,
    max_bytes: int | None = None,
    deadline: float | None = None,
    chunk_size: int = 64 * 1024,
    clock: Callable[[], float] = time.perf_counter,
) -> str:
    """Hash payload in chunks so time and work budgets are actually enforced."""

    algo = normalize_algorithm(algorithm)
    if max_bytes is not None and len(payload) > max_bytes:
        raise AuditError(
            E_HASH_WORK_LIMIT,
            f"component digest work {len(payload)} bytes exceeds {max_bytes}",
            "payload",
        )
    if algo == "sha256":
        hasher = hashlib.sha256()
    else:
        if key is None:
            raise AuditError(E_HASH_ALGO, "hmac-sha256 requires a key", "key")
        hasher = hmac.new(key, digestmod=hashlib.sha256)
    for start in range(0, len(payload), chunk_size):
        if deadline is not None and clock() >= deadline:
            raise AuditError(E_TIMEOUT, "batch wall-clock budget exceeded during digest computation")
        hasher.update(payload[start : start + chunk_size])
    if deadline is not None and clock() >= deadline:
        raise AuditError(E_TIMEOUT, "batch wall-clock budget exceeded after digest computation")
    return hasher.hexdigest()


def derive_hmac_over_digest(digest_hex: str, key: bytes) -> str:
    return hmac.new(key, bytes.fromhex(digest_hex), hashlib.sha256).hexdigest()
