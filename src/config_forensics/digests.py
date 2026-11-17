"""Evidence digest validation for sha256 and hmac-sha256."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from .canonical import observation_digest_payload
from .codes import Code

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_ALGORITHM_ALIASES = {
    "sha256": "sha256",
    "sha-256": "sha256",
    "hmac-sha256": "hmac-sha256",
    "hmac_sha256": "hmac-sha256",
    "hmacsha256": "hmac-sha256",
}
DEFAULT_ALGORITHMS = ("sha256", "hmac-sha256")
EXPECTED_BITS = {"sha256": 256, "hmac-sha256": 256}


@dataclass
class DigestCheck:
    ok: bool
    code: str | None = None
    algorithm: str | None = None
    digest: str | None = None
    bits: int | None = None
    detail: str = ""


def normalize_algorithm(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _ALGORITHM_ALIASES.get(value.strip().lower())


def _decode_hmac_key(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    return None


def verify_observation_digest(
    *,
    normalized_path: str,
    value: object,
    algorithm: object,
    digest: object,
    declared_bits: object,
    allowed_algorithms: tuple[str, ...] | list[str] | set[str],
    hmac_key: object = None,
) -> DigestCheck:
    normalized_algorithm = normalize_algorithm(algorithm)
    if normalized_algorithm is None:
        return DigestCheck(False, Code.ALGORITHM_UNSUPPORTED, detail="unknown algorithm")

    whitelist = {normalize_algorithm(item) for item in allowed_algorithms}
    whitelist.discard(None)
    if normalized_algorithm not in whitelist:
        return DigestCheck(
            False,
            Code.ALGORITHM_UNSUPPORTED,
            algorithm=normalized_algorithm,
        )

    bits: int
    if declared_bits is None:
        bits = EXPECTED_BITS[normalized_algorithm]
    elif isinstance(declared_bits, bool) or not isinstance(declared_bits, int):
        return DigestCheck(
            False,
            Code.DIGEST_WIDTH_MISMATCH,
            algorithm=normalized_algorithm,
            detail="declared bit width must be an integer",
        )
    else:
        bits = declared_bits

    if bits != EXPECTED_BITS[normalized_algorithm]:
        return DigestCheck(
            False,
            Code.DIGEST_WIDTH_MISMATCH,
            algorithm=normalized_algorithm,
            bits=bits,
            detail=f"expected {EXPECTED_BITS[normalized_algorithm]} bits",
        )

    if not isinstance(digest, str) or not digest:
        return DigestCheck(
            False,
            Code.DIGEST_INVALID,
            algorithm=normalized_algorithm,
            bits=bits,
        )

    declared_algorithm = None
    digest_text = digest.strip()
    if ":" in digest_text:
        prefix, digest_text = digest_text.split(":", 1)
        declared_algorithm = normalize_algorithm(prefix)
        if declared_algorithm != normalized_algorithm:
            return DigestCheck(
                False,
                Code.ALGORITHM_DECLARATION_MISMATCH,
                algorithm=normalized_algorithm,
                bits=bits,
            )

    if not _HEX.fullmatch(digest_text):
        return DigestCheck(
            False,
            Code.DIGEST_INVALID,
            algorithm=normalized_algorithm,
            digest=digest_text.lower(),
            bits=bits,
        )
    if len(digest_text) != bits // 4:
        return DigestCheck(
            False,
            Code.DIGEST_WIDTH_MISMATCH,
            algorithm=normalized_algorithm,
            digest=digest_text.lower(),
            bits=bits,
            detail=f"expected {bits // 4} hexadecimal characters",
        )

    payload = observation_digest_payload(normalized_path, value)
    if normalized_algorithm == "sha256":
        expected = hashlib.sha256(payload).hexdigest()
    else:
        key = _decode_hmac_key(hmac_key)
        if key is None:
            return DigestCheck(
                False,
                Code.HMAC_KEY_MISSING,
                algorithm=normalized_algorithm,
                bits=bits,
            )
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, digest_text.lower()):
        return DigestCheck(
            False,
            Code.TAMPERED,
            algorithm=normalized_algorithm,
            digest=digest_text.lower(),
            bits=bits,
        )
    return DigestCheck(
        True,
        algorithm=normalized_algorithm,
        digest=digest_text.lower(),
        bits=bits,
    )
