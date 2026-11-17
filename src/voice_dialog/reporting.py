"""Stable report projection and report persistence helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import atomic_write_json, canonicalize, stable_hash


def stable_report_view(report: dict[str, Any]) -> dict[str, Any]:
    """Remove execution-only data, leaving full-vs-incremental comparable state."""
    view = {
        key: value
        for key, value in report.items()
        if key not in {"cache", "runtime", "report_fingerprint"}
    }
    snapshots = dict(view.get("snapshots", {}))
    snapshots.pop("errors", None)
    view["snapshots"] = snapshots
    return canonicalize(view)


def attach_report_fingerprint(report: dict[str, Any]) -> dict[str, Any]:
    report = canonicalize(report)
    report.pop("report_fingerprint", None)
    report["report_fingerprint"] = stable_hash(stable_report_view(report))
    return canonicalize(report)


def write_report(path: Path, report: dict[str, Any]) -> None:
    atomic_write_json(path, attach_report_fingerprint(report))
