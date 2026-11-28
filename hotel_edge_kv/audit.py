from __future__ import annotations

from typing import Any

from .scheduler import Scheduler, TERMINAL_STATUSES


def _issue(
    kind: str,
    message: str,
    *,
    time: int | None = None,
    block_id: int | None = None,
    request_id: str | None = None,
    ticket_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    result = {
        "kind": kind,
        "message": message,
        "time": time,
        "block_id": block_id,
        "request_id": request_id,
        "ticket_id": ticket_id,
    }
    result.update(extra)
    return result


def audit_scheduler(scheduler: Scheduler) -> dict[str, Any]:
    """Independently replay the block ledger and check all requested invariants."""
    issues: list[dict[str, Any]] = []

    owners: dict[int, dict[str, Any]] = {}
    intervals: dict[int, list[dict[str, Any]]] = {}
    double_allocations: list[dict[str, Any]] = []
    duplicate_releases: list[dict[str, Any]] = []
    orphan_releases: list[dict[str, Any]] = []
    ownership_mismatches: list[dict[str, Any]] = []
    released_reallocations: list[dict[str, Any]] = []
    last_event_by_block: dict[int, str] = {}

    for entry in sorted(scheduler.block_ledger, key=lambda item: item.sequence):
        if entry.event == "allocate":
            last_event_by_block[entry.block_id] = "allocate"
            if entry.block_id in owners:
                previous = owners[entry.block_id]
                issue = _issue(
                    "block_occupied_by_two_requests",
                    "block was allocated while still owned by another request",
                    time=entry.time,
                    block_id=entry.block_id,
                    request_id=entry.request_id,
                    ticket_id=entry.ticket_id,
                    previous_request_id=previous["request_id"],
                    previous_ticket_id=previous["ticket_id"],
                )
                issues.append(issue)
                double_allocations.append(issue)
            else:
                source_request = entry.details.get("reused_from_request_id")
                if source_request is not None:
                    released_reallocations.append(
                        {
                            "time": entry.time,
                            "block_id": entry.block_id,
                            "request_id": entry.request_id,
                            "ticket_id": entry.ticket_id,
                            "from_request_id": source_request,
                            "from_ticket_id": entry.details.get(
                                "reused_from_ticket_id"
                            ),
                            "release_reason": entry.details.get(
                                "reused_from_release_reason"
                            ),
                        }
                    )
            owners[entry.block_id] = {
                "time": entry.time,
                "sequence": entry.sequence,
                "request_id": entry.request_id,
                "ticket_id": entry.ticket_id,
                "reason": entry.reason,
            }
        elif entry.event == "release":
            owner = owners.get(entry.block_id)
            if last_event_by_block.get(entry.block_id) == "release":
                issue = _issue(
                    "duplicate_block_release",
                    "two consecutive release events were observed for one block",
                    time=entry.time,
                    block_id=entry.block_id,
                    request_id=entry.request_id,
                    ticket_id=entry.ticket_id,
                )
                issues.append(issue)
                duplicate_releases.append(issue)
            last_event_by_block[entry.block_id] = "release"
            if owner is None:
                issue = _issue(
                    "release_without_owner",
                    "release event found for a block with no active owner",
                    time=entry.time,
                    block_id=entry.block_id,
                    request_id=entry.request_id,
                    ticket_id=entry.ticket_id,
                )
                issues.append(issue)
                orphan_releases.append(issue)
                continue
            if (
                owner["request_id"] != entry.request_id
                or owner["ticket_id"] != entry.ticket_id
            ):
                issue = _issue(
                    "release_owner_mismatch",
                    "release event does not match the active block owner",
                    time=entry.time,
                    block_id=entry.block_id,
                    request_id=entry.request_id,
                    ticket_id=entry.ticket_id,
                    expected_request_id=owner["request_id"],
                    expected_ticket_id=owner["ticket_id"],
                )
                issues.append(issue)
                ownership_mismatches.append(issue)
            intervals.setdefault(entry.block_id, []).append(
                {
                    **owner,
                    "released_at": entry.time,
                    "release_sequence": entry.sequence,
                    "release_reason": entry.reason,
                }
            )
            del owners[entry.block_id]
        else:
            issues.append(
                _issue(
                    "unknown_block_ledger_event",
                    f"unknown block ledger event: {entry.event}",
                    time=entry.time,
                    block_id=entry.block_id,
                    request_id=entry.request_id,
                    ticket_id=entry.ticket_id,
                )
            )

    for block_id in sorted(intervals):
        ordered = sorted(intervals[block_id], key=lambda item: item["time"])
        for previous, current in zip(ordered, ordered[1:]):
            if current["time"] < previous["released_at"]:
                issues.append(
                    _issue(
                        "overlapping_block_ownership",
                        "consecutive ownership intervals overlap",
                        time=current["time"],
                        block_id=block_id,
                        request_id=current["request_id"],
                        ticket_id=current["ticket_id"],
                        previous_request_id=previous["request_id"],
                        previous_ticket_id=previous["ticket_id"],
                        previous_released_at=previous["released_at"],
                    )
                )

    terminal_at: dict[str, int] = {}
    for entry in scheduler.lifecycle:
        if entry.event in {"completed", "cancelled"}:
            runtime = scheduler.runtimes[entry.request_id]
            if runtime.status in TERMINAL_STATUSES:
                terminal_at[entry.request_id] = entry.time

    terminal_residuals: list[dict[str, Any]] = []
    for block_id, record in sorted(scheduler.block_records.items()):
        if record.owner_request_id is None:
            continue
        runtime = scheduler.runtimes.get(record.owner_request_id)
        if runtime is None:
            issue = _issue(
                "block_owner_unknown",
                "block has an owner that is not in the request set",
                block_id=block_id,
                request_id=record.owner_request_id,
                ticket_id=record.owner_ticket_id,
            )
            issues.append(issue)
            continue
        if runtime.status in TERMINAL_STATUSES:
            issue = _issue(
                "terminal_request_still_owns_block",
                "terminal request has a block that was never returned",
                time=runtime.terminal_at,
                block_id=block_id,
                request_id=runtime.request_id,
                ticket_id=scheduler.request_by_id[runtime.request_id].ticket_id,
            )
            issues.append(issue)
            terminal_residuals.append(issue)

    conservation_violations: list[dict[str, Any]] = []
    budget_violations: list[dict[str, Any]] = []
    logical_timeline = scheduler.dense_memory_timeline()
    for sample in logical_timeline:
        if (
            sample["used_blocks"] + sample["free_blocks"]
            != scheduler.config.total_blocks
        ):
            issue = _issue(
                "block_conservation",
                "used + free does not equal total blocks",
                time=sample["time"],
                used_blocks=sample["used_blocks"],
                free_blocks=sample["free_blocks"],
                total_blocks=scheduler.config.total_blocks,
            )
            issues.append(issue)
            conservation_violations.append(issue)
        if sample["used_bytes"] > scheduler.config.vram_budget_bytes:
            issue = _issue(
                "vram_budget_exceeded",
                "used bytes exceed the configured VRAM budget",
                time=sample["time"],
                used_bytes=sample["used_bytes"],
                budget_bytes=scheduler.config.vram_budget_bytes,
            )
            issues.append(issue)
            budget_violations.append(issue)
        seen_owner_blocks: set[int] = set()
        active_ids: set[str] = set()
        for owner in sample["block_owners"]:
            block_id = int(owner["block_id"])
            if block_id in seen_owner_blocks:
                issues.append(
                    _issue(
                        "duplicate_owner_in_snapshot",
                        "block appears twice in one occupancy snapshot",
                        time=sample["time"],
                        block_id=block_id,
                        request_id=owner.get("request_id"),
                        ticket_id=owner.get("ticket_id"),
                    )
                )
            seen_owner_blocks.add(block_id)
            request_id = owner.get("request_id")
            if request_id is not None:
                active_ids.add(str(request_id))
        for request_id in active_ids:
            if request_id in terminal_at and sample["time"] >= terminal_at[request_id]:
                issues.append(
                    _issue(
                        "block_not_returned_after_terminal",
                        "a block remains owned at or after the request terminal time",
                        time=sample["time"],
                        request_id=request_id,
                        ticket_id=scheduler.request_by_id[request_id].ticket_id,
                        terminal_at=terminal_at[request_id],
                    )
                )

    tokens_after_cancel: list[dict[str, Any]] = []
    for token in scheduler.token_events:
        runtime = scheduler.runtimes[token["request_id"]]
        if runtime.status != "cancelled":
            continue
        effective_at = runtime.cancel_effective_at
        if effective_at is not None and token["time"] >= effective_at:
            issue = _issue(
                "token_after_cancellation",
                "cancelled request generated a token at or after cancellation",
                time=token["time"],
                request_id=token["request_id"],
                ticket_id=token["ticket_id"],
                cancel_effective_at=effective_at,
                token_index=token["token_index"],
            )
            issues.append(issue)
            tokens_after_cancel.append(issue)

    duplicate_readmission: list[dict[str, Any]] = []
    for runtime in scheduler.runtimes.values():
        if runtime.status != "cancelled":
            continue
        if runtime.request_id in scheduler.queue:
            issue = _issue(
                "cancelled_request_readmitted",
                "cancelled request remains in the admission queue",
                request_id=runtime.request_id,
                ticket_id=scheduler.request_by_id[runtime.request_id].ticket_id,
            )
            issues.append(issue)
            duplicate_readmission.append(issue)
        for entry in scheduler.lifecycle:
            if (
                entry.request_id == runtime.request_id
                and entry.event == "slot_acquired"
                and runtime.cancel_effective_at is not None
                and entry.time > runtime.cancel_effective_at
            ):
                issue = _issue(
                    "cancelled_request_readmitted",
                    "cancelled request acquired a batch slot again",
                    time=entry.time,
                    request_id=runtime.request_id,
                    ticket_id=entry.ticket_id,
                )
                issues.append(issue)
                duplicate_readmission.append(issue)

    cancelled_reuse = [
        link
        for link in scheduler.reuse_links
        if link.get("from_was_cancelled")
    ]
    check_specs = [
        (
            "block_conservation",
            "used_blocks + free_blocks == total_blocks at every sampled logical time",
            len(logical_timeline),
            not conservation_violations,
        ),
        (
            "vram_budget",
            "used bytes never exceed vram_budget_bytes",
            len(logical_timeline),
            not budget_violations,
        ),
        (
            "no_double_allocation",
            "no block is allocated while still owned",
            len(scheduler.block_ledger),
            not double_allocations,
        ),
        (
            "no_duplicate_release",
            "no block is released more than once",
            len(scheduler.block_ledger),
            not duplicate_releases,
        ),
        (
            "no_terminal_residual_blocks",
            "completed/cancelled requests own no residual blocks",
            len(scheduler.block_records),
            not terminal_residuals,
        ),
        (
            "no_token_after_cancel",
            "cancelled requests produce no token at or after cancellation",
            len(scheduler.token_events),
            not tokens_after_cancel,
        ),
        (
            "no_cancelled_readmission",
            "cancelled requests never re-enter the queue or batch",
            len(scheduler.runtimes),
            not duplicate_readmission,
        ),
        (
            "block_ledger_consistency",
            "every release matches its current owner",
            len(scheduler.block_ledger),
            not orphan_releases and not ownership_mismatches,
        ),
    ]

    return {
        "status": "ok" if not issues else "issues_detected",
        "checks": [
            {
                "name": name,
                "description": description,
                "checked_items": checked,
                "passed": passed,
            }
            for name, description, checked, passed in check_specs
        ],
        "issues": issues,
        "issue_count": len(issues),
        "summary": {
            "allocations": sum(
                1 for entry in scheduler.block_ledger if entry.event == "allocate"
            ),
            "releases": sum(
                1 for entry in scheduler.block_ledger if entry.event == "release"
            ),
            "unique_blocks_seen": len(
                {entry.block_id for entry in scheduler.block_ledger}
            ),
            "released_block_reallocations": len(released_reallocations),
            "blocks_reused_from_cancellations": len(cancelled_reuse),
            "bytes_reused_from_cancellations": (
                len(cancelled_reuse) * scheduler.config.block_bytes
            ),
            "double_allocations": len(double_allocations),
            "duplicate_releases": len(duplicate_releases),
            "orphan_releases": len(orphan_releases),
            "terminal_residual_blocks": len(terminal_residuals),
            "tokens_after_cancellation": len(tokens_after_cancel),
            "cancelled_readmissions": len(duplicate_readmission),
        },
        "released_block_reallocations": released_reallocations,
        "block_ownership_intervals": {
            str(block_id): values
            for block_id, values in sorted(intervals.items())
        },
    }
