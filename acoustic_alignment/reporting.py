"""Report digest reconciliation and atomic report commit."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codes import Code
from .ledger import LedgerStore, build_report_from_ledger, write_report_atomic


def reconcile_existing_report(
    report_path: str | Path,
    ledger: LedgerStore,
    input_digest: str,
    task: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Check the previous report and ledger summary before replacing the report.

    Returns ``(report, recovery)``. When a digest/count inconsistency is found,
    the most recent valid ledger backup is reloaded and the report is rebuilt
    and atomically committed before this function returns.
    """
    path = Path(report_path)
    problems: list[str] = []
    old_report: dict[str, Any] | None = None

    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("report root is not an object")
            old_report = loaded
        except (json.JSONDecodeError, OSError, ValueError):
            problems.append("report_json_invalid")

    actual_count = len(ledger.current_input_results(input_digest))
    declared_count = len(task.tracks) if task is not None else actual_count

    if old_report is not None:
        if old_report.get("input_digest") != input_digest:
            problems.append("input_digest_mismatch")
        if old_report.get("ledger_digest") != ledger.ledger_digest:
            problems.append("ledger_digest_mismatch")
        if old_report.get("track_count") != actual_count:
            problems.append("report_track_count_mismatch")
        if old_report.get("declared_track_count") not in (None, declared_count):
            problems.append("declared_track_count_mismatch")
        tracks = old_report.get("tracks")
        if not isinstance(tracks, list) or len(tracks) != old_report.get("track_count"):
            problems.append("report_track_entries_mismatch")
        if actual_count != declared_count:
            problems.append("ledger_declared_count_mismatch")

    if ledger.restored_from_backup:
        problems.append("ledger_restored_from_valid_backup")

    if not problems:
        return None, None

    restored = False
    if "ledger_digest_mismatch" in problems or ledger.restored_from_backup:
        restored = ledger.restored_from_backup
        if restored:
            digest = ledger._read_and_verify()  # noqa: SLF001 - recovery boundary
            ledger.ledger_digest = digest

    recovery = {
        "code": Code.E_DIGEST,
        "problems": sorted(set(problems)),
        "restored_valid_ledger_backup": bool(restored or ledger.restored_from_backup),
        "ledger_digest": ledger.ledger_digest,
        "actual_track_count": actual_count,
        "declared_track_count": declared_count,
    }
    report = build_report_from_ledger(ledger, input_digest, task, recovery=recovery)
    write_report_atomic(report, path)
    return report, recovery


def commit_report(
    report_path: str | Path,
    ledger: LedgerStore,
    input_digest: str,
    task: Any,
    extra_recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _, reconciliation = reconcile_existing_report(report_path, ledger, input_digest, task)
    recovery = reconciliation or extra_recovery
    if reconciliation and extra_recovery:
        recovery = {**reconciliation, **extra_recovery, "code": Code.E_DIGEST}
    report = build_report_from_ledger(ledger, input_digest, task, recovery=recovery)
    write_report_atomic(report, report_path)
    return report
