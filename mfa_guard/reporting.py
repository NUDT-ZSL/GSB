"""Integrity-checked, atomic JSON reporting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, read_json
from .errors import Codes, MFAError
from .util import canonical_json_bytes

REPORT_FORMAT = "mfa-decision-report-v1"


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    body = dict(report)
    body.setdefault("format", REPORT_FORMAT)
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    sealed = dict(body)
    sealed["integrity"] = {"algorithm": "sha256", "sha256": digest}
    return sealed


def verify_sealed_report(report: dict[str, Any]) -> bool:
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        return False
    expected = integrity.get("sha256")
    body = dict(report)
    body.pop("integrity", None)
    actual = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return expected == actual


def write_report(path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    sealed = seal_report(report)
    atomic_write_json(path, sealed)
    parsed = read_json(path)
    if not isinstance(parsed, dict) or not verify_sealed_report(parsed):
        raise MFAError(
            Codes.REPORT_INVALID,
            "report failed post-write integrity verification",
            {"path": str(path)},
        )
    return sealed

