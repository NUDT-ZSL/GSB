"""Validation and immutable parsing of JSONL change-log packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import EngineError, fail
from .models import UnifiedIndex
from .util import (
    digest_file,
    digest_json,
    read_json,
    require_dict,
    require_int,
    require_list,
    require_str,
    safe_child,
)


@dataclass(frozen=True)
class ChangeEvent:
    seq: int
    base_table: str
    key: str
    op: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    logical_clock_ms: int
    content_digest: str
    segment_id: str
    segment_file: str
    line_number: int

    @property
    def trigger_source(self) -> str:
        return (
            f"log:{self.segment_id}:seq={self.seq}:"
            f"table={self.base_table}:key={self.key}:op={self.op}"
        )

    def core_payload(self) -> dict[str, Any]:
        return {
            "after": self.after,
            "base_table": self.base_table,
            "before": self.before,
            "key": self.key,
            "logical_clock_ms": self.logical_clock_ms,
            "op": self.op,
            "seq": self.seq,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.core_payload(), "content_digest": self.content_digest}


@dataclass(frozen=True)
class LogPackage:
    path: Path
    manifest_sha256: str
    events: tuple[ChangeEvent, ...]
    segment_hashes: dict[str, str]
    start_seq: int
    end_seq: int

    @property
    def log_digest(self) -> str:
        return digest_json([event.content_digest for event in self.events])


def _validate_row(value: Any, *, field: str, code: str = "E_LOG_INVALID") -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_dict(value, field, code=code)
    return row


def parse_logs(path: Path) -> LogPackage:
    manifest_path = path / "manifest.json" if path.is_dir() else path
    root = manifest_path.parent
    manifest = read_json(manifest_path, code="E_LOG_INVALID")
    manifest = require_dict(manifest, "log manifest", code="E_LOG_INVALID")
    if manifest.get("format") != "mv-logs-v1":
        raise fail("E_LOG_INVALID", "log manifest must declare format=mv-logs-v1")
    raw_segments = require_list(manifest.get("segments"), "segments", code="E_LOG_INVALID")
    if not raw_segments:
        raise fail("E_LOG_INVALID", "log manifest must contain at least one segment")
    events: list[ChangeEvent] = []
    segment_hashes: dict[str, str] = {}
    previous_seq = 0
    previous_clock: int | None = None
    seen_segment_ids: set[str] = set()
    for segment_index, raw_segment in enumerate(raw_segments):
        segment = require_dict(raw_segment, f"segments[{segment_index}]", code="E_LOG_INVALID")
        relative = require_str(segment.get("file"), "segment.file", code="E_LOG_INVALID")
        segment_id = require_str(segment.get("segment_id"), "segment.segment_id", code="E_LOG_INVALID")
        if segment_id in seen_segment_ids:
            raise fail(
                "E_LOG_INVALID",
                "segment_id must be unique within a log package",
                segment_id=segment_id,
            )
        seen_segment_ids.add(segment_id)
        expected_hash = require_str(segment.get("sha256"), "segment.sha256", code="E_LOG_INVALID")
        if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
            raise fail(
                "E_LOG_INVALID",
                "segment sha256 must be a lowercase SHA-256 digest",
                segment_id=segment_id,
            )
        segment_path = safe_child(root, relative)
        if not segment_path.is_file():
            raise fail(
                "E_INPUT_NOT_FOUND",
                "log segment file does not exist",
                segment_id=segment_id,
                file=relative,
            )
        actual_hash = digest_file(segment_path)
        if actual_hash != expected_hash:
            raise fail(
                "E_LOG_HASH_MISMATCH",
                "log segment SHA-256 does not match the manifest",
                segment_id=segment_id,
                expected=expected_hash,
                actual=actual_hash,
            )
        segment_hashes[relative] = actual_hash
        start_seq = require_int(
            segment.get("start_seq"),
            "segment.start_seq",
            minimum=1,
            code="E_LOG_INVALID",
        )
        end_seq = require_int(
            segment.get("end_seq"),
            "segment.end_seq",
            minimum=1,
            code="E_LOG_INVALID",
        )
        if start_seq > end_seq:
            raise fail(
                "E_LOG_SEQUENCE",
                "segment start_seq exceeds end_seq",
                segment_id=segment_id,
            )
        if start_seq <= previous_seq:
            raise fail(
                "E_LOG_SEQUENCE",
                "segments overlap or are not ordered by seq",
                segment_id=segment_id,
                start_seq=start_seq,
                previous_seq=previous_seq,
            )
        text = segment_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) != end_seq - start_seq + 1:
            raise fail(
                "E_LOG_INVALID",
                "segment record count does not match its declared seq range",
                segment_id=segment_id,
                expected=end_seq - start_seq + 1,
                actual=len(lines),
            )
        for offset, line in enumerate(lines, start=1):
            if not line.strip():
                raise fail(
                    "E_LOG_INVALID",
                    "blank lines are not allowed in JSONL change logs",
                    segment_id=segment_id,
                    line=offset,
                )
            try:
                raw_event = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON number: {value}")
                    ),
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise fail(
                    "E_LOG_INVALID",
                    "log line is not valid JSON",
                    segment_id=segment_id,
                    line=offset,
                    detail=str(exc),
                )
            event_obj = require_dict(raw_event, "change event", code="E_LOG_INVALID")
            sequence = require_int(
                event_obj.get("seq"),
                "change.seq",
                minimum=1,
                code="E_LOG_SEQUENCE",
            )
            expected_sequence = start_seq + offset - 1
            if sequence != expected_sequence:
                raise fail(
                    "E_LOG_SEQUENCE",
                    "change seq does not match the declared segment range",
                    segment_id=segment_id,
                    line=offset,
                    expected=expected_sequence,
                    actual=sequence,
                )
            if previous_seq != 0 and sequence != previous_seq + 1:
                raise fail(
                    "E_LOG_SEQUENCE",
                    "change seq must increase contiguously across all segments",
                    segment_id=segment_id,
                    line=offset,
                    expected=previous_seq + 1,
                    actual=sequence,
                )
            base_table = require_str(
                event_obj.get("base_table"),
                "change.base_table",
                code="E_LOG_INVALID",
            )
            key = require_str(event_obj.get("key"), "change.key", code="E_LOG_INVALID")
            operation = require_str(event_obj.get("op"), "change.op", code="E_LOG_INVALID")
            if operation not in {"insert", "update", "delete"}:
                raise fail(
                    "E_LOG_INVALID",
                    "change op must be insert, update, or delete",
                    segment_id=segment_id,
                    line=offset,
                    op=operation,
                )
            before = _validate_row(event_obj.get("before"), field="change.before")
            after = _validate_row(event_obj.get("after"), field="change.after")
            if operation == "insert" and (before is not None or after is None):
                raise fail(
                    "E_LOG_INVALID",
                    "insert requires before=null and an object after-image",
                    segment_id=segment_id,
                    line=offset,
                )
            if operation == "delete" and (before is None or after is not None):
                raise fail(
                    "E_LOG_INVALID",
                    "delete requires a before-image and after=null",
                    segment_id=segment_id,
                    line=offset,
                )
            if operation == "update" and (before is None or after is None):
                raise fail(
                    "E_LOG_INVALID",
                    "update requires both before and after images",
                    segment_id=segment_id,
                    line=offset,
                )
            clock = require_int(
                event_obj.get("logical_clock_ms"),
                "change.logical_clock_ms",
                minimum=0,
                code="E_LOG_CLOCK_ORDER",
            )
            if previous_clock is not None and clock < previous_clock:
                raise fail(
                    "E_LOG_CLOCK_ORDER",
                    "logical clock must be nondecreasing across the log chain",
                    segment_id=segment_id,
                    line=offset,
                    previous=previous_clock,
                    actual=clock,
                )
            previous_clock = clock
            supplied_digest = require_str(
                event_obj.get("content_digest"),
                "change.content_digest",
                code="E_LOG_DIGEST",
            )
            event = ChangeEvent(
                seq=sequence,
                base_table=base_table,
                key=key,
                op=operation,
                before=before,
                after=after,
                logical_clock_ms=clock,
                content_digest=supplied_digest,
                segment_id=segment_id,
                segment_file=relative,
                line_number=offset,
            )
            computed = digest_json(event.core_payload())
            if supplied_digest != computed:
                raise fail(
                    "E_LOG_DIGEST",
                    "change content_digest does not match the canonical event payload",
                    segment_id=segment_id,
                    line=offset,
                    seq=sequence,
                    expected=computed,
                    actual=supplied_digest,
                )
            events.append(event)
            previous_seq = sequence
        if previous_seq != end_seq:
            raise fail(
                "E_LOG_SEQUENCE",
                "segment did not end at its declared end_seq",
                segment_id=segment_id,
            )
    _validate_change_chain(events)
    return LogPackage(
        path=manifest_path,
        manifest_sha256=digest_file(manifest_path),
        events=tuple(events),
        segment_hashes=segment_hashes,
        start_seq=events[0].seq,
        end_seq=events[-1].seq,
    )


def _validate_change_chain(events: list[ChangeEvent]) -> None:
    """Validate in-package before/after continuity without requiring database state."""

    if not events:
        return
    mirror: dict[tuple[str, str], dict[str, Any] | None] = {}
    for index, event in enumerate(events):
        row_id = (event.base_table, event.key)
        initialize_from_first = index == 0 and event.seq != 1
        current = mirror.get(row_id)
        if not initialize_from_first:
            if event.op == "insert" and current is not None:
                raise fail(
                    "E_LOG_STATE_MISMATCH",
                    "insert follows an existing in-package row image",
                    seq=event.seq,
                    segment_id=event.segment_id,
                    table=event.base_table,
                    key=event.key,
                )
            if event.op in {"update", "delete"} and current != event.before:
                raise fail(
                    "E_LOG_STATE_MISMATCH",
                    "before-image is not the previous in-package row image",
                    seq=event.seq,
                    segment_id=event.segment_id,
                    table=event.base_table,
                    key=event.key,
                    expected=current,
                    actual=event.before,
                )
        if event.op == "delete":
            mirror[row_id] = None
        else:
            mirror[row_id] = event.after


def validate_log_against_index(events: Iterable[ChangeEvent], index: UnifiedIndex) -> None:
    """Validate row tables and every column needed by filters/aggregates."""

    required_columns: dict[str, set[str]] = {}
    numeric_columns: dict[str, set[str]] = {}
    for spec in index.active_views().values():
        from .expressions import FilterExpression

        parsed_filter = FilterExpression(spec.filter_text, error_code="E_VIEW_FILTER")
        for table in spec.base_tables:
            required_columns.setdefault(table, set()).update(parsed_filter.columns)
        for rule in spec.aggregate_rules:
            if rule.function in {"sum", "sum_sq"}:
                for table in spec.base_tables:
                    required_columns.setdefault(table, set()).add(rule.column)
                    numeric_columns.setdefault(table, set()).add(rule.column)
            elif rule.column != "*":
                for table in spec.base_tables:
                    required_columns.setdefault(table, set()).add(rule.column)

    for event in events:
        if event.base_table not in index.base_schema:
            raise fail(
                "E_LOG_UNKNOWN_TABLE",
                "change references a base table absent from the unified index",
                seq=event.seq,
                segment_id=event.segment_id,
                base_table=event.base_table,
            )
        for image_name, image in (("before", event.before), ("after", event.after)):
            if image is None:
                continue
            missing = sorted(required_columns.get(event.base_table, set()) - set(image))
            if missing:
                raise fail(
                    "E_LOG_ROW_SCHEMA",
                    "change image is missing columns required by views",
                    seq=event.seq,
                    segment_id=event.segment_id,
                    image=image_name,
                    missing=missing,
                )
            for column in sorted(numeric_columns.get(event.base_table, set())):
                value = image.get(column)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise fail(
                        "E_LOG_ROW_TYPE",
                        "aggregate column must be numeric or null",
                        seq=event.seq,
                        segment_id=event.segment_id,
                        image=image_name,
                        column=column,
                        value=value,
                    )

