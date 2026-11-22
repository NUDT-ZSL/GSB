"""Versioned whole-snapshot envelopes with checksum verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import sha256_json
from .codes import E_SNAPSHOT_CORRUPT
from .errors import raise_business


LEDGER_CATEGORY = "write_ledger"
RESULT_CATEGORY = "row_results"


def snapshot_checksum(category: str, version: int, payload: Any) -> str:
    return sha256_json(
        {
            "category": category,
            "version": version,
            "payload": payload,
        }
    )


def make_snapshot(category: str, version: int, payload: Any) -> dict[str, Any]:
    return {
        "category": category,
        "version": int(version),
        "checksum": snapshot_checksum(category, int(version), payload),
        "payload": payload,
    }


@dataclass(frozen=True)
class VerifiedSnapshot:
    category: str
    version: int
    checksum: str
    payload: Any


def verify_snapshot(
    raw: Any,
    *,
    expected_category: str,
    case_id: str | None,
) -> VerifiedSnapshot:
    if not isinstance(raw, dict):
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=case_id,
            segment="snapshot",
            message="snapshot envelope must be an object",
            snapshot_category=expected_category,
            snapshot_version=None,
        )
    category = raw.get("category")
    version = raw.get("version")
    checksum = raw.get("checksum")
    if category != expected_category:
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=case_id,
            segment="snapshot.category",
            message="snapshot category mismatch",
            snapshot_category=expected_category,
            actual_category=category,
            snapshot_version=version,
        )
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=case_id,
            segment="snapshot.version",
            message="snapshot version must be a non-negative integer",
            snapshot_category=category,
            snapshot_version=version,
        )
    if "payload" not in raw or not isinstance(checksum, str):
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=case_id,
            segment="snapshot.checksum",
            message="snapshot is missing payload or checksum",
            snapshot_category=category,
            snapshot_version=version,
        )
    expected = snapshot_checksum(category, version, raw["payload"])
    if checksum != expected:
        raise_business(
            E_SNAPSHOT_CORRUPT,
            case_id=case_id,
            segment="snapshot.checksum",
            message="snapshot content checksum mismatch",
            snapshot_category=category,
            snapshot_version=version,
            expected_checksum=expected,
            provided_checksum=checksum,
        )
    return VerifiedSnapshot(
        category=category,
        version=version,
        checksum=checksum,
        payload=raw["payload"],
    )
