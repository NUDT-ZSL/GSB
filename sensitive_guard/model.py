"""Shared domain model and canonical serialization helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


STATUSES = ("plaintext", "masked", "invisible")
STATUS_RANK = {"invisible": 0, "masked": 1, "plaintext": 2}
STATUS_ALIASES = {
    "plaintext": "plaintext",
    "plain": "plaintext",
    "明文": "plaintext",
    "masked": "masked",
    "desensitized": "masked",
    "脱敏": "masked",
    "invisible": "invisible",
    "hidden": "invisible",
    "不可见": "invisible",
}
VERSIONS = ("v1", "v2")

BUSINESS_CODE_ORDER = (
    "E_RECORD",
    "E_SYNC",
    "E_FIELDS",
    "E_TIMEOUT",
    "E_CYCLE",
    "E_VERSION_CONFLICT",
    "E_DOWNGRADE",
    "E_LEAK",
    "E_INTERNAL",
)


class DatasetSchemaError(ValueError):
    """Raised when a description-set envelope is not usable."""


@dataclass(frozen=True)
class FieldRecord:
    record_index: int
    path: str
    sources: tuple[str, ...]
    status: str
    policy_id: str
    version: str
    transitive: bool
    raw: dict[str, Any]


def normalize_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return STATUS_ALIASES.get(value.strip().lower())


def normalize_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    version = value.strip().lower()
    return version if version in VERSIONS else None


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_sync_marker(records: list[Any], injection: Any) -> str:
    """Hash records as an order-independent multiset plus injection metadata."""

    normalized_records: list[str] = []
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("sources"), list):
            normalized = dict(record)
            normalized["sources"] = sorted(
                record["sources"],
                key=lambda source: canonical_json(source),
            )
        else:
            normalized = record
        normalized_records.append(canonical_json(normalized))
    canonical_records = sorted(normalized_records)
    payload = canonical_json(
        {
            "injection": injection if injection is not None else {},
            "records": canonical_records,
        }
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sort_codes(codes: set[str] | list[str]) -> list[str]:
    code_set = set(codes)

    def key(code: str) -> tuple[int, str]:
        try:
            return (BUSINESS_CODE_ORDER.index(code), code)
        except ValueError:
            return (len(BUSINESS_CODE_ORDER), code)

    return sorted(code_set, key=key)
