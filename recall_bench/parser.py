"""Interaction log and item dictionary parsing, validation and indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .errors import (
    E_ITEM_MISSING,
    E_LOG_DUPLICATE,
    E_LOG_INVALID,
    E_LOG_ORDER,
)


@dataclass
class Failure:
    line_no: int
    code: str
    user_id: str
    item_id: str
    reason: str
    raw: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "code": self.code,
            "user_id": self.user_id,
            "item_id": self.item_id,
            "reason": self.reason,
            "raw": self.raw,
        }


@dataclass
class ParsedData:
    interactions: list[dict[str, Any]] = field(default_factory=list)
    item_terms: dict[str, list[str]] = field(default_factory=dict)
    user_interactions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    user_items: dict[str, list[str]] = field(default_factory=dict)
    item_users: dict[str, list[str]] = field(default_factory=dict)
    user_count: int = 0
    item_count: int = 0
    interaction_count: int = 0
    user_interaction_counts: dict[str, int] = field(default_factory=dict)
    item_interaction_counts: dict[str, int] = field(default_factory=dict)
    item_latest_epoch: dict[str, float] = field(default_factory=dict)
    time_span: dict[str, Any] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)

    def failure_dicts(self) -> list[dict[str, Any]]:
        return [failure.to_dict() for failure in self.failures]


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise ValueError("timestamp must be an epoch number or ISO 8601 string")


def _read_jsonl(path: str) -> list[tuple[int, str, dict[str, Any] | None, str | None]]:
    rows: list[tuple[int, str, dict[str, Any] | None, str | None]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                import json

                rows.append((line_no, stripped, json.loads(stripped), None))
            except ValueError as exc:
                rows.append((line_no, stripped, None, str(exc)))
    return rows


def parse_item_dictionary(path: str, failures: list[Failure]) -> dict[str, list[str]]:
    item_terms: dict[str, list[str]] = {}
    for line_no, raw, obj, json_error in _read_jsonl(path):
        if json_error is not None or not isinstance(obj, dict):
            failures.append(
                Failure(line_no, E_LOG_INVALID, "", "", f"dictionary json error: {json_error}", raw)
            )
            continue
        item_id = obj.get("item_id")
        terms = obj.get("terms")
        if not isinstance(item_id, str) or not item_id.strip():
            failures.append(Failure(line_no, E_LOG_INVALID, "", str(item_id), "empty item_id", raw))
            continue
        if not isinstance(terms, list) or not terms or any(
            not isinstance(term, str) or not term.strip() for term in terms
        ):
            failures.append(
                Failure(line_no, E_LOG_INVALID, "", item_id, "terms must be a non-empty string list", raw)
            )
            continue
        clean_terms = [term.strip() for term in terms]
        if item_id in item_terms:
            failures.append(Failure(line_no, E_LOG_DUPLICATE, "", item_id, "duplicate dictionary item", raw))
            continue
        item_terms[item_id] = clean_terms
    return item_terms


def parse_interactions(
    log_path: str,
    item_terms: dict[str, list[str]],
    allowed_types: list[str],
) -> ParsedData:
    data = ParsedData()
    data.item_terms = item_terms
    data.failures = []

    seen: set[tuple[str, str, str, str]] = set()
    last_ts_by_user: dict[str, datetime] = {}

    for line_no, raw, obj, json_error in _read_jsonl(log_path):
        if json_error is not None or not isinstance(obj, dict):
            data.failures.append(
                Failure(line_no, E_LOG_INVALID, "", "", f"json error: {json_error}", raw)
            )
            continue
        user_id = obj.get("user_id")
        item_id = obj.get("item_id")
        ts_raw = obj.get("timestamp")
        interaction_type = obj.get("interaction_type")
        if not isinstance(user_id, str) or not user_id.strip():
            data.failures.append(Failure(line_no, E_LOG_INVALID, "", str(item_id), "empty user_id", raw))
            continue
        if not isinstance(item_id, str) or not item_id.strip():
            data.failures.append(Failure(line_no, E_LOG_INVALID, user_id, "", "empty item_id", raw))
            continue
        if interaction_type not in allowed_types:
            data.failures.append(
                Failure(line_no, E_LOG_INVALID, user_id, item_id,
                        f"interaction_type {interaction_type!r} not in {allowed_types}", raw)
            )
            continue
        try:
            timestamp = parse_timestamp(ts_raw)
        except (ValueError, TypeError, OSError) as exc:
            data.failures.append(
                Failure(line_no, E_LOG_INVALID, user_id, item_id, f"bad timestamp: {exc}", raw)
            )
            continue

        dedup_key = (user_id, item_id, timestamp.isoformat(), interaction_type)
        if dedup_key in seen:
            data.failures.append(Failure(line_no, E_LOG_DUPLICATE, user_id, item_id, "duplicate row", raw))
            continue
        seen.add(dedup_key)

        previous = last_ts_by_user.get(user_id)
        if previous is not None and timestamp < previous:
            data.failures.append(
                Failure(line_no, E_LOG_ORDER, user_id, item_id,
                        f"timestamp {timestamp.isoformat()} before {previous.isoformat()}", raw)
            )
            continue
        last_ts_by_user[user_id] = timestamp

        if item_id not in item_terms:
            data.failures.append(
                Failure(line_no, E_ITEM_MISSING, user_id, item_id, "item missing from dictionary", raw)
            )
            continue

        record = {
            "user_id": user_id,
            "item_id": item_id,
            "timestamp": timestamp.isoformat(),
            "ts_epoch": timestamp.timestamp(),
            "interaction_type": interaction_type,
        }
        data.interactions.append(record)

    _build_indices(data)
    return data


def _build_indices(data: ParsedData) -> None:
    user_buckets: dict[str, list[dict[str, Any]]] = {}
    item_users: dict[str, list[str]] = {}
    user_item_set: dict[str, set[str]] = {}
    item_counts: dict[str, int] = {}
    item_latest: dict[str, float] = {}
    min_ts: float | None = None
    max_ts: float | None = None

    for record in data.interactions:
        user_id = record["user_id"]
        item_id = record["item_id"]
        user_buckets.setdefault(user_id, []).append(record)
        user_item_set.setdefault(user_id, set()).add(item_id)
        item_users.setdefault(item_id, []).append(user_id)
        item_counts[item_id] = item_counts.get(item_id, 0) + 1
        epoch = record["ts_epoch"]
        item_latest[item_id] = max(item_latest.get(item_id, epoch), epoch)
        min_ts = epoch if min_ts is None else min(min_ts, epoch)
        max_ts = epoch if max_ts is None else max(max_ts, epoch)

    for user_id in sorted(user_buckets):
        records = sorted(user_buckets[user_id], key=lambda row: (row["ts_epoch"], row["item_id"]))
        data.user_interactions[user_id] = records
        data.user_items[user_id] = sorted(user_item_set[user_id])
        data.user_interaction_counts[user_id] = len(records)

    for item_id in sorted(item_users):
        data.item_users[item_id] = sorted(item_users[item_id])
        data.item_interaction_counts[item_id] = item_counts[item_id]
        data.item_latest_epoch[item_id] = item_latest[item_id]

    data.user_count = len(data.user_interactions)
    data.item_count = len(data.item_terms)
    data.interaction_count = len(data.interactions)
    if min_ts is not None and max_ts is not None:
        data.time_span = {
            "start": datetime.fromtimestamp(min_ts, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(max_ts, tz=timezone.utc).isoformat(),
            "duration_seconds": max_ts - min_ts,
        }


def parse_all(log_path: str, dictionary_path: str, allowed_types: list[str]) -> ParsedData:
    dictionary_failures: list[Failure] = []
    item_terms = parse_item_dictionary(dictionary_path, dictionary_failures)
    data = parse_interactions(log_path, item_terms, allowed_types)
    data.failures = dictionary_failures + data.failures
    data.failures.sort(key=lambda failure: (failure.line_no, failure.code, failure.item_id))
    return data
