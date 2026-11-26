from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any, Iterable

from .models import AuditDecision, Attempt, Directory, SimulationResult, WriteEvent


def directory_path(
    dir_id: str, dirs: dict[str, Directory]
) -> list[Directory]:
    path: list[Directory] = []
    seen: set[str] = set()
    current: str | None = dir_id
    while current is not None:
        if current in seen:
            break
        seen.add(current)
        directory = dirs[current]
        path.append(directory)
        current = directory.parent_id
    return path


def resolve_quota(
    directory: Directory,
    file_key: str,
    clock_ms: int,
    unified: dict[str, dict[str, Any]],
) -> tuple[int | None, str | None]:
    if directory.tombstoned:
        return None, "directory_tombstoned"
    rule = unified.get(f"dir:{directory.dir_id}")
    if rule is not None and clock_ms >= int(rule["effective_at_ms"]):
        return int(rule["quota_bytes"]), None
    if file_key in directory.ext_quotas:
        return int(directory.ext_quotas[file_key]), None
    if directory.base_quota is not None:
        return int(directory.base_quota), None
    return None, "no_quota_for_file_key"


def simulate(
    dirs: dict[str, Directory],
    events: Iterable[WriteEvent],
    unified: dict[str, dict[str, Any]],
) -> SimulationResult:
    ordered_events = sorted(events, key=lambda event: event.seq)
    consumed: dict[tuple[str, str], int] = defaultdict(int)
    last_quota: dict[tuple[str, str], int] = {}
    updated_seq: dict[tuple[str, str], int] = {}
    decisions: list[AuditDecision] = []
    max_clock = 0

    for event in ordered_events:
        max_clock = max(max_clock, event.clock_ms)
        if event.dir_id not in dirs:
            offered = Attempt(
                dir_id=event.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                quota=None,
                consumed_before=0,
                available_before=None,
                status="FAIL",
                reason="unknown_directory",
            )
            decisions.append(
                AuditDecision(
                    seq=event.seq,
                    dir_id=event.dir_id,
                    file_key=event.file_key,
                    bytes=event.bytes,
                    digest=event.digest,
                    clock_ms=event.clock_ms,
                    status="FAIL",
                    fail_dir=event.dir_id,
                    reason="unknown_directory",
                    attempts=[offered],
                )
            )
            continue

        path = directory_path(event.dir_id, dirs)
        attempts: list[Attempt] = []
        failure: Attempt | None = None
        for directory in path:
            key = (directory.dir_id, event.file_key)
            before = consumed[key]
            quota, reason = resolve_quota(
                directory, event.file_key, event.clock_ms, unified
            )
            available = None if quota is None else quota - before
            attempt = Attempt(
                dir_id=directory.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                quota=quota,
                consumed_before=before,
                available_before=available,
                status="OK",
            )
            if quota is None:
                attempt.status = "FAIL"
                attempt.reason = reason or "no_quota_for_file_key"
                attempts.append(attempt)
                failure = attempt
                break
            if available is None or available < event.bytes:
                attempt.status = "FAIL"
                attempt.reason = "quota_exceeded"
                attempts.append(attempt)
                failure = attempt
                break
            attempts.append(attempt)

        if failure is not None:
            for attempt in attempts:
                if attempt.status == "OK":
                    attempt.rolled_back = True
            decisions.append(
                AuditDecision(
                    seq=event.seq,
                    dir_id=event.dir_id,
                    file_key=event.file_key,
                    bytes=event.bytes,
                    digest=event.digest,
                    clock_ms=event.clock_ms,
                    status="FAIL",
                    fail_dir=failure.dir_id,
                    reason=failure.reason,
                    deducted_nodes=0,
                    deducted_bytes=0,
                    attempts=attempts,
                )
            )
            continue

        for attempt in attempts:
            key = (attempt.dir_id, event.file_key)
            consumed[key] += event.bytes
            if attempt.quota is not None:
                last_quota[key] = attempt.quota
            updated_seq[key] = event.seq
        decisions.append(
            AuditDecision(
                seq=event.seq,
                dir_id=event.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                digest=event.digest,
                clock_ms=event.clock_ms,
                status="OK",
                deducted_nodes=len(attempts),
                deducted_bytes=len(attempts) * event.bytes,
                attempts=attempts,
            )
        )

    ledger: dict[tuple[str, str], dict[str, Any]] = {}
    for key, used in sorted(consumed.items()):
        if used <= 0:
            continue
        directory = dirs.get(key[0])
        final_quota = last_quota.get(key)
        if directory is not None:
            current_quota, _ = resolve_quota(
                directory, key[1], max_clock, unified
            )
            if current_quota is not None:
                final_quota = current_quota
        final_quota = int(final_quota or used)
        ledger[key] = {
            "dir_id": key[0],
            "file_key": key[1],
            "consumed": used,
            "remaining": max(0, final_quota - used),
            "quota": final_quota,
            "updated_seq": updated_seq.get(key),
        }
    return SimulationResult(ledger=ledger, audit=decisions, max_clock_ms=max_clock)


def subtree_and_ancestor_closure(
    roots: Iterable[str],
    dirs: dict[str, Directory],
    reasons: dict[str, str] | None = None,
) -> dict[str, str]:
    """Mark changed roots, their dependent subtrees and ancestor chains.

    The dependency edge points from a child directory to its parent.  A rule
    on a directory therefore invalidates the directory itself, descendants
    that roll quota through it, and ancestors whose deductions may be reached
    differently after a child fails or succeeds.
    """

    reasons = reasons or {}
    children: dict[str, list[str]] = defaultdict(list)
    for directory in dirs.values():
        if directory.parent_id is not None:
            children[directory.parent_id].append(directory.dir_id)

    affected: dict[str, str] = {}
    for root in sorted(set(roots)):
        if root not in dirs:
            continue
        direct_reason = reasons.get(root, "dependency_changed")
        queue: deque[str] = deque([root])
        while queue:
            current = queue.popleft()
            affected.setdefault(current, direct_reason)
            queue.extend(sorted(children.get(current, ())))

        current_parent = dirs[root].parent_id
        while current_parent is not None:
            affected.setdefault(current_parent, "ancestor_invalidated")
            current_parent = dirs[current_parent].parent_id

    return {key: affected[key] for key in sorted(affected)}


def path_intersects(path: list[Directory], affected: set[str]) -> bool:
    return any(directory.dir_id in affected for directory in path)


def audit_attempts(attempts_json: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(attempts_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []
