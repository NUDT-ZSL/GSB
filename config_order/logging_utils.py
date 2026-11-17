"""Append-only structured JSONL diagnostic logging."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import E_CHAIN, Issue


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_event(log_path: str | Path, event: dict[str, Any]) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"logged_at": utc_now(), **event}
    line = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_issues(
    log_path: str | Path,
    *,
    phase: str,
    business_code: str,
    issues: Iterable[Issue],
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    issue_list = [issue.as_dict() for issue in issues]
    append_event(
        log_path,
        {
            "event": "DIAGNOSTIC_ERROR",
            "phase": phase,
            "business_code": business_code,
            "session_id": session_id,
            "issues": issue_list,
            **(extra or {}),
        },
    )


def log_result_issues(
    log_path: str | Path,
    result: dict[str, Any],
    *,
    phase: str = "diagnose",
) -> None:
    issues: list[Issue] = []
    for environment in result.get("environments", []):
        for key_report in environment.get("keys", []):
            code = key_report.get("code")
            if code == "OK":
                continue
            decision = key_report.get("decision", {})
            issues.append(
                Issue(
                    code=code,
                    field_path=(
                        f"environments.{environment['environment_id']}."
                        f"keys.{key_report['key']}"
                    ),
                    message=decision.get("message", code),
                    environment_id=environment["environment_id"],
                    config_key=key_report["key"],
                    source_id=(
                        decision.get("involved_sources", [None])[0]
                        if decision.get("involved_sources")
                        else None
                    ),
                    detail={
                        "status": decision.get("status"),
                        "trace_step": 2,
                    },
                )
            )
    for issue_dict in (
        result.get("chain_issues", [])
        if not any(issue.code == E_CHAIN for issue in issues)
        else []
    ):
        instruction = Issue(
            code=issue_dict["code"],
            field_path=issue_dict["field_path"],
            message=issue_dict["message"],
            environment_id=issue_dict.get("environment_id"),
            config_key=issue_dict.get("config_key"),
            source_id=issue_dict.get("source_id"),
            detail=issue_dict.get("detail"),
        )
        issues.append(instruction)
    for code in sorted({issue.code for issue in issues}):
        grouped = [issue for issue in issues if issue.code == code]
        log_issues(
            log_path,
            phase=phase,
            business_code=code,
            issues=grouped,
            session_id=result.get("session_id"),
        )
