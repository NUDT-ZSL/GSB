from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import REPORT_FORMAT
from .canonical import atomic_write_json, digest_value
from .errors import ReportVersionError


def make_report_version_id(
    input_version_id: str, revision: int, aggregate_digests: list[str]
) -> str:
    return "rep_" + digest_value(
        {
            "input_version_id": input_version_id,
            "revision": revision,
            "aggregate_digests": aggregate_digests,
        }
    )[:16]


def atomic_write_report(
    path: str | Path,
    report: dict[str, Any],
    *,
    expected_existing_version_id: str | None = None,
) -> dict[str, Any]:
    """Write a report temp-file/fsync/replace sequence with version CAS."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_version: str | None = None
    if target.exists():
        try:
            with target.open("r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ReportVersionError(
                f"existing report is not valid JSON: {target}",
                code="E_REPORT_VERSION",
                details={"path": str(target)},
            ) from exc
        existing_format = existing.get("report_format")
        if existing_format != REPORT_FORMAT:
            raise ReportVersionError(
                f"unsupported existing report format {existing_format!r}",
                code="E_REPORT_FORMAT",
                details={
                    "path": str(target),
                    "existing_format": existing_format,
                    "expected_format": REPORT_FORMAT,
                },
            )
        existing_version = existing.get("version_id")
        if (
            expected_existing_version_id is not None
            and existing_version != expected_existing_version_id
        ):
            raise ReportVersionError(
                "report version compare-and-swap failed",
                code="E_REPORT_VERSION_CONFLICT",
                details={
                    "path": str(target),
                    "expected_version_id": expected_existing_version_id,
                    "existing_version_id": existing_version,
                },
            )
    elif expected_existing_version_id is not None:
        raise ReportVersionError(
            "expected an existing report for version compare, but file is absent",
            code="E_REPORT_VERSION_CONFLICT",
            details={"path": str(target)},
        )

    report = dict(report)
    report["report_format"] = REPORT_FORMAT
    report["supersedes_version_id"] = existing_version
    if not report.get("version_id"):
        report["version_id"] = "rep_" + digest_value(report)[:16]

    payload = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        if target.exists():
            with target.open("r", encoding="utf-8") as handle:
                current = json.load(handle)
            current_version = current.get("version_id")
            if (
                expected_existing_version_id is not None
                and current_version != expected_existing_version_id
            ):
                raise ReportVersionError(
                    "report changed before atomic replacement",
                    code="E_REPORT_VERSION_CONFLICT",
                    details={
                        "expected_version_id": expected_existing_version_id,
                        "current_version_id": current_version,
                    },
                )

        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            pass
        raise

    return {
        "path": str(target),
        "version_id": report["version_id"],
        "supersedes_version_id": existing_version,
        "bytes": len(payload),
    }


def write_interrupt_marker(
    path: str | Path, marker: dict[str, Any]
) -> None:

    atomic_write_json(path, marker)
