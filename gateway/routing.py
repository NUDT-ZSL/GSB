from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from .errors import GatewayError, bad_sample


STABLE_HASH_V1 = "sha256_mod_v1"
FNV1A32 = "fnv1a32"
SUPPORTED_KEY_RULES = frozenset({STABLE_HASH_V1, FNV1A32})

_KEY_RULE_ALIASES = {
    "sha256_mod_v1": STABLE_HASH_V1,
    "sha256": STABLE_HASH_V1,
    "stable_hash_v1": STABLE_HASH_V1,
    "stable": STABLE_HASH_V1,
    "fnv1a32": FNV1A32,
    "fnv1a": FNV1A32,
}


def canonical_key_rule(value: Any) -> str:
    if not isinstance(value, str):
        raise bad_sample("partition key rule must be a string", field="partition_key_rule")
    rule = _KEY_RULE_ALIASES.get(value.strip().lower())
    if rule is None:
        raise bad_sample(
            "unsupported partition key rule",
            field="partition_key_rule",
            value=value,
            supported=sorted(SUPPORTED_KEY_RULES),
        )
    return rule


def normalize_partition_key(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise bad_sample(
            "partition key cannot be normalized to a stable string",
            field="partition_key",
            value=value,
        )
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        raise bad_sample("partition key must not be empty", field="partition_key")
    return text


def _fnv1a32(text: str) -> int:
    digest = 0x811C9DC5
    for byte in text.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def stable_hash_int(text: str, seed: int = 0) -> int:
    payload = f"{seed}\x1f{text}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def route_partition(
    topic_name: str,
    partition_count: int,
    partition_key: Any,
    key_rule: str = STABLE_HASH_V1,
) -> int:
    if partition_count <= 0:
        raise bad_sample(
            "partition count must be positive",
            field="partition_count",
            topic=topic_name,
            value=partition_count,
        )
    normalized = normalize_partition_key(partition_key)
    rule = canonical_key_rule(key_rule)
    hash_input = f"{topic_name}\x1f{normalized}"
    if rule == FNV1A32:
        return _fnv1a32(hash_input) % partition_count
    digest = hashlib.sha256(hash_input.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % partition_count


def partition_id(topic_name: str, partition: int) -> str:
    return f"{topic_name}/{partition}"


def parse_partition_id(value: str) -> tuple[str, int]:
    try:
        topic, raw_partition = value.rsplit("/", 1)
        partition = int(raw_partition)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GatewayError(
            "E_SEQUENCE_VIOLATION",
            "invalid partition identifier",
            {"partition_id": value},
        ) from exc
    return topic, partition
