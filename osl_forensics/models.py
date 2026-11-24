"""Typed parse results and stable JSON-ready reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .codes import Code


@dataclass
class Diagnostic:
    code: str
    message: str
    offset: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "offset": self.offset,
            "details": _json_safe(self.details),
        }


@dataclass
class SegmentInfo:
    key: str
    name: str
    path_names: list[str]
    depth: int
    record_offset: int
    name_offset: int
    type_offset: int
    compression_offset: int
    payload_offset_field_offset: int
    declared_length_field_offset: int
    algo_offset: int
    digest_length_offset: int | None
    digest_offset: int
    name_length: int
    segment_type: int
    compression: int
    payload_offset: int
    declared_length: int
    algo: int
    digest_length: int
    declared_digest: str
    absolute_payload_offset: int
    base_offset: int
    content_digest: str | None = None
    actual_digest: str | None = None
    digest_valid: bool | None = None
    status: str = Code.OK
    error_code: str | None = None
    error_offset: int | None = None
    refs: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    payload_length_available: int = 0

    def identity(self) -> tuple[str, str | None, int, int]:
        return (
            self.key,
            self.actual_digest,
            self.status,
            self.error_offset if self.error_offset is not None else -1,
        )

    def to_ledger_dict(self) -> dict[str, Any]:
        return {
            "segment_key": self.key,
            "status": self.status,
            "error_code": self.error_code,
            "error_offset": self.error_offset,
            "processed_watermark": max(
                0, self.absolute_payload_offset + self.declared_length
            ),
            "content_digest": self.content_digest,
            "payload_digest": self.actual_digest,
            "declared_digest": self.declared_digest,
            "digest_algo": self.algo,
            "cumulative_digest": self.actual_digest or self.declared_digest,
            "details": {
                "name": self.name,
                "depth": self.depth,
                "segment_type": self.segment_type,
                "compression": self.compression,
                "payload_offset": self.payload_offset,
                "declared_length": self.declared_length,
                "absolute_payload_offset": self.absolute_payload_offset,
                "refs": sorted(self.refs),
                "duplicate_of": self.duplicate_of,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


@dataclass
class ParseResult:
    file_id: str
    ok: bool
    policy_fingerprint: str
    header_layout: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    segments: list[SegmentInfo] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    expanded_bytes: int = 0
    max_depth_reached: int = 0
    elapsed_seconds: float = 0.0
    header_size: int = 40

    @property
    def primary(self) -> Diagnostic | None:
        if not self.diagnostics:
            return None
        return sorted(
            self.diagnostics,
            key=lambda d: (
                _PRIORITY.get(d.code, 90),
                d.offset if d.offset is not None else 1 << 63,
                d.message,
            ),
        )[0]

    @property
    def primary_code(self) -> str:
        primary = self.primary
        return primary.code if primary else Code.OK

    @property
    def error_offset(self) -> int | None:
        primary = self.primary
        return primary.offset if primary else None

    @property
    def ledger_eligible(self) -> bool:
        """A structurally complete round can advance segment attestation state."""

        return not any(d.code in _FATAL_LEDGER_CODES for d in self.diagnostics)

    def normalized_segments(self) -> list[SegmentInfo]:
        return sorted(self.segments, key=lambda s: s.key)

    def to_dict(self) -> dict[str, Any]:
        primary = self.primary
        return {
            "file_id": self.file_id,
            "ok": self.ok,
            "primary_code": self.primary_code,
            "error_offset": self.error_offset,
            "policy_fingerprint": self.policy_fingerprint,
            "header_layout": self.header_layout,
            "header_size": self.header_size,
            "expanded_bytes": self.expanded_bytes,
            "max_depth_reached": self.max_depth_reached,
            "elapsed_seconds": round(self.elapsed_seconds, 9),
            "diagnostics": [d.to_dict() for d in sorted_diagnostics(self.diagnostics)],
            "segments": [s.to_dict() for s in self.normalized_segments()],
            "edges": sorted([list(edge) for edge in self.edges]),
        }


_PRIORITY = {
    Code.E_HEADER: 10,
    Code.E_BOUNDS: 11,
    Code.E_TIMEOUT: 12,
    Code.E_QUOTA: 13,
    Code.E_DEPTH: 14,
    Code.E_DUP_NAME: 20,
    Code.E_SELFREF: 21,
    Code.E_OVERLAP: 22,
    Code.E_CHECK_ALGO: 23,
    Code.E_CHECKSUM: 24,
    Code.E_CYCLE: 25,
}

_FATAL_LEDGER_CODES = frozenset(
    {Code.E_HEADER, Code.E_BOUNDS, Code.E_TIMEOUT, Code.E_QUOTA}
)


def sorted_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    return sorted(
        diagnostics,
        key=lambda d: (
            _PRIORITY.get(d.code, 90),
            d.offset if d.offset is not None else -1,
            d.message,
            _stable_details(d.details),
        ),
    )


def _stable_details(details: dict[str, Any]) -> str:
    import json

    return json.dumps(details, sort_keys=True, ensure_ascii=False, default=str)


def _json_safe(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    return value
