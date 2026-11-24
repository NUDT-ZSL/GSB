"""Versioned parsing/ledger policy."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping

from .canonical import stable_json_bytes, sha256_hex


DEFAULT_HMAC_KEY = b"osl-prototype-fixed-local-hmac-key-v1"


@dataclass(frozen=True)
class HeaderLayout:
    name: str
    size: int
    reserved_size: int


OSL40_LAYOUT = HeaderLayout(name="osl40-packed", size=40, reserved_size=14)
LITERAL_RESERVED16_LAYOUT = HeaderLayout(
    name="literal-fields-reserved16", size=42, reserved_size=16
)


@dataclass(frozen=True)
class Policy:
    """Whitelists and limits whose changes invalidate old ledger conclusions."""

    policy_version: str = "policy-v1"
    allowed_format_versions: tuple[int, ...] = (1, 2)
    allowed_segment_types: tuple[int, ...] = (1, 2, 3, 4, 5)
    allowed_compressions: tuple[int, ...] = (0x00, 0x01, 0x02, 0x10)
    algorithm_bits: Mapping[int, int] = field(
        default_factory=lambda: {0: 0, 1: 256, 2: 256}
    )
    allowed_algorithms: tuple[int, ...] = (0, 1, 2)
    hmac_key: bytes = DEFAULT_HMAC_KEY
    max_depth: int = 4
    max_expanded_bytes: int = 8 * 1024 * 1024
    header_layout: HeaderLayout = OSL40_LAYOUT
    digest_length_prefix: bool = True

    def fingerprint(self) -> str:
        doc = {
            "policy_version": self.policy_version,
            "format_versions": list(self.allowed_format_versions),
            "segment_types": list(self.allowed_segment_types),
            "compressions": list(self.allowed_compressions),
            "algorithms": list(self.allowed_algorithms),
            "algorithm_bits": {str(k): v for k, v in sorted(self.algorithm_bits.items())},
            "header_layout": self.header_layout.name,
            "header_size": self.header_layout.size,
            "reserved_size": self.header_layout.reserved_size,
            "digest_length_prefix": self.digest_length_prefix,
            "max_depth": self.max_depth,
            "max_expanded_bytes": self.max_expanded_bytes,
        }
        return sha256_hex(stable_json_bytes(doc))

    def with_changes(self, **changes: object) -> "Policy":
        if "hmac_key" in changes and isinstance(changes["hmac_key"], str):
            changes["hmac_key"] = changes["hmac_key"].encode("utf-8")
        return replace(self, **changes)  # type: ignore[arg-type]
