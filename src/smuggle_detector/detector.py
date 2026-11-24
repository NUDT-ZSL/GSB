"""Per-connection detection, rule gating and evidence ledger construction."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .constants import CODE_TO_PATTERN, E_INTERNAL
from .httpstream import Deadline, StreamResult, reassemble_direction
from .models import MessageRecord
from .rules import RuleCatalog, RuleView


def _rule_matches_code(rule_patterns: tuple[str, ...], code: str) -> bool:
    expected = CODE_TO_PATTERN.get(code)
    return expected is not None and expected in rule_patterns


def _hit_from_candidate(candidate: object, rule: object, view: RuleView) -> dict:
    pattern = CODE_TO_PATTERN[candidate.code]
    return {
        "code": candidate.code,
        "direction": candidate.direction,
        "byte_offset": candidate.offset,
        "range": [candidate.offset, candidate.end_offset],
        "sync_id": candidate.sync_id,
        "message_index": candidate.message_index,
        "evidence": candidate.evidence,
        "rule_name": rule.canonical_name,
        "rule_generation": view.generation,
        "rule_effective_ms": rule.effective_ms,
        "rule_direction": rule.direction,
        "rule_pattern": pattern,
        "snapshot_fingerprint": view.fingerprint,
    }


def _apply_rules(stream: StreamResult, view: RuleView) -> tuple[list[dict], int]:
    hits: list[dict] = []
    suppressed = 0
    rules = view.rules_for_direction(stream.direction)
    for candidate in stream.candidates:
        matching = [
            rule
            for rule in rules
            if _rule_matches_code(rule.patterns, candidate.code)
        ]
        if not matching:
            suppressed += 1
            continue
        for rule in matching:
            hits.append(_hit_from_candidate(candidate, rule, view))
    hits.sort(
        key=lambda item: (
            item["direction"],
            item["byte_offset"],
            item["code"],
            item["rule_name"],
            item.get("sync_id") or "",
        )
    )
    return hits, suppressed


def connection_time_ms(records: Iterable[MessageRecord]) -> int:
    return max((record.ts_ms for record in records), default=0)


def detect_connection(
    connection_id: str,
    records: Iterable[MessageRecord],
    catalog: RuleCatalog,
    *,
    timeout_ms: int,
) -> dict:
    deadline = Deadline(timeout_ms)
    record_list = list(records)
    time_ms = connection_time_ms(record_list)
    view = catalog.select(time_ms)

    grouped: dict[str, list[MessageRecord]] = defaultdict(list)
    for record in record_list:
        deadline.check()
        grouped[record.direction].append(record)

    stream_results: dict[str, StreamResult] = {}
    all_hits: list[dict] = []
    all_errors: list[dict] = []
    suppressed_count = 0
    for direction in ("c2s", "s2c"):
        deadline.check()
        stream = reassemble_direction(
            grouped.get(direction, []),
            connection_id=connection_id,
            direction=direction,
            deadline=deadline,
        )
        stream_results[direction] = stream
        hits, suppressed = _apply_rules(stream, view)
        all_hits.extend(hits)
        all_errors.extend(stream.errors)
        suppressed_count += suppressed

    all_hits.sort(
        key=lambda item: (
            item["direction"],
            item["byte_offset"],
            item["code"],
            item["rule_name"],
            item.get("sync_id") or "",
        )
    )
    all_errors.sort(
        key=lambda item: (
            item["direction"],
            item.get("byte_offset", -1),
            item["code"],
        )
    )

    directions_doc = {}
    for direction in ("c2s", "s2c"):
        stream = stream_results[direction]
        direction_hits = [hit for hit in all_hits if hit["direction"] == direction]
        directions_doc[direction] = stream.to_doc(direction_hits)

    messages = [
        message.to_doc()
        for direction in ("c2s", "s2c")
        for message in stream_results[direction].messages
    ]
    duplicate_count = sum(
        stream.duplicate_count for stream in stream_results.values()
    )
    return {
        "connection_id": connection_id,
        "status": "ok",
        "connection_time_ms": time_ms,
        "rule_view": view.to_doc(),
        "messages": messages,
        "hits": all_hits,
        "errors": all_errors,
        "directions": directions_doc,
        "duplicate_count": duplicate_count,
        "suppressed_candidate_count": suppressed_count,
        "elapsed_ms": round(deadline.elapsed_ms(), 3),
    }


def failed_connection_result(
    connection_id: str,
    *,
    code: str,
    detail: str,
    elapsed_ms: float = 0.0,
    timeout_ms: int | None = None,
) -> dict:
    error = {
        "code": code,
        "detail": detail,
        "byte_offset": -1,
        "range": [],
    }
    if timeout_ms is not None:
        error["timeout_ms"] = timeout_ms
        error["elapsed_ms"] = round(elapsed_ms, 3)
    return {
        "connection_id": connection_id,
        "status": "timeout" if code == "E_TIMEOUT" else "failed",
        "connection_time_ms": 0,
        "rule_view": {
            "generation": 0,
            "activation_ms": 0,
            "fingerprint": "",
            "rule_count": 0,
            "rules": [],
        },
        "messages": [],
        "hits": [],
        "errors": [error],
        "directions": {},
        "duplicate_count": 0,
        "suppressed_candidate_count": 0,
        "elapsed_ms": round(elapsed_ms, 3),
    }


def internal_failure_result(
    connection_id: str,
    *,
    detail: str,
    elapsed_ms: float,
) -> dict:
    result = failed_connection_result(
        connection_id,
        code=E_INTERNAL,
        detail=detail,
        elapsed_ms=elapsed_ms,
    )
    result["status"] = "failed"
    return result

