"""Algorithm dispatch, hexadecimal validation and digest evidence helpers."""

from __future__ import annotations

import hashlib
import hmac as hmac_lib
import re
from dataclasses import dataclass

from .codes import ALGORITHM_WHITELIST_VERSION, E_HASH_ALGO, E_HASH_HEX

HEX_RE = re.compile(r"^[0-9a-fA-F]*$")

ALGORITHM_REGISTRY = {
    "sha256": {"width_bits": 256, "hashlib_name": "sha256", "hmac": False},
    "hmac-sha256": {"width_bits": 256, "hashlib_name": "sha256", "hmac": True},
    "sha512": {"width_bits": 512, "hashlib_name": "sha512", "hmac": False},
    "hmac-sha512": {"width_bits": 512, "hashlib_name": "sha512", "hmac": True},
}

ALGORITHM_ALIASES = {
    "sha_256": "sha256",
    "sha-256": "sha256",
    "sha2-256": "sha256",
    "hmac_sha256": "hmac-sha256",
    "hmac-sha_256": "hmac-sha256",
    "hmac_sha_256": "hmac-sha256",
    "hmac-sha-256": "hmac-sha256",
    "sha_512": "sha512",
    "sha-512": "sha512",
    "hmac_sha512": "hmac-sha512",
    "hmac-sha-512": "hmac-sha512",
}


@dataclass(frozen=True)
class DigestValidation:
    ok: bool
    code: str | None
    algorithm_declared: str | None
    algorithm_normalized: str | None
    width_bits_expected: int | None
    width_bits_actual: int | None
    digest_normalized: str | None
    case_profile: str | None
    message: str | None = None


def normalize_algorithm(value: object) -> tuple[str | None, dict | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    collapsed = re.sub(r"\s+", "", value.strip().lower())
    normalized = ALGORITHM_ALIASES.get(collapsed, collapsed)
    if normalized not in ALGORITHM_REGISTRY:
        return normalized, None
    return normalized, ALGORITHM_REGISTRY[normalized]


def case_profile(digest: str) -> str:
    has_upper = any("A" <= char <= "F" for char in digest)
    has_lower = any("a" <= char <= "f" for char in digest)
    if has_upper and has_lower:
        return "mixed"
    if has_upper:
        return "upper"
    return "lower"


def validate_digest(digest: object, algorithm: object) -> DigestValidation:
    algorithm_declared = algorithm if isinstance(algorithm, str) else None
    normalized_algorithm, spec = normalize_algorithm(algorithm)
    if spec is None:
        return DigestValidation(
            False,
            E_HASH_ALGO,
            algorithm_declared,
            normalized_algorithm,
            None,
            None,
            None,
            None,
            f"algorithm is not in whitelist {ALGORITHM_WHITELIST_VERSION}",
        )

    if not isinstance(digest, str) or not digest:
        return DigestValidation(
            False,
            E_HASH_HEX,
            algorithm_declared,
            normalized_algorithm,
            spec["width_bits"],
            None,
            None,
            None,
            "digest must be a non-empty hexadecimal string",
        )

    if HEX_RE.fullmatch(digest) is None:
        return DigestValidation(
            False,
            E_HASH_HEX,
            algorithm_declared,
            normalized_algorithm,
            spec["width_bits"],
            len(digest) * 4,
            None,
            None,
            "digest contains non-hexadecimal characters",
        )

    actual_width = len(digest) * 4
    normalized_digest = digest.lower()
    if actual_width != spec["width_bits"]:
        return DigestValidation(
            False,
            E_HASH_ALGO,
            algorithm_declared,
            normalized_algorithm,
            spec["width_bits"],
            actual_width,
            normalized_digest,
            case_profile(digest),
            "digest bit width does not match declared algorithm",
        )

    return DigestValidation(
        True,
        None,
        algorithm_declared,
        normalized_algorithm,
        spec["width_bits"],
        actual_width,
        normalized_digest,
        case_profile(digest),
        None,
    )


def prefix_evidence(claims, prefix_chars: int = 12):
    evidence = []
    for claim in claims:
        digest = claim.get("digest_normalized")
        evidence.append(
            {
                "snapshot_id": claim.get("snapshot_id"),
                "batch_id": claim.get("batch_id"),
                "digest_generation": claim.get("digest_generation"),
                "algorithm_normalized": claim.get("algorithm_normalized"),
                "digest_prefix": digest[:prefix_chars] if digest else None,
                "prefix_length": prefix_chars,
            }
        )
    return evidence


def compute_payload_digest(
    payload: bytes, algorithm: str, hmac_key: bytes | None = None
) -> str:
    normalized, spec = normalize_algorithm(algorithm)
    if spec is None:
        raise ValueError(f"unsupported algorithm: {algorithm!r}")
    if spec["hmac"]:
        if hmac_key is None:
            raise ValueError("HMAC digest computation requires a local key")
        return hmac_lib.new(hmac_key, payload, spec["hashlib_name"]).hexdigest()
    return hashlib.new(spec["hashlib_name"], payload).hexdigest()
