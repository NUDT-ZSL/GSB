"""Pure deterministic state transitions used by refresh, replay, and verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .errors import EngineError, fail
from .expressions import FilterExpression
from .logs import ChangeEvent
from .models import UnifiedIndex, ViewSpec
from .util import canonical_json, digest_json


def canonical_metrics() -> dict[str, Any]:
    return {"count": 0, "sum": 0.0, "sum_sq": 0.0}


@dataclass
class ViewState:
    view_name: str
    generation: int
    status: str
    applied_seq: int = 0
    logical_clock_ms: int = 0
    visible_rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=canonical_metrics)
    result_digest: str = ""
    recompute_count: int = 0
    last_recomputed_seq: int = 0
    last_trigger: str = ""
    last_path: list[str] = field(default_factory=list)
    last_recomputed_segment: str = ""

    def row_entries(self) -> list[dict[str, Any]]:
        return [
            {
                "key": entry["key"],
                "row": entry["row"],
                "source": entry["source"],
                "table": entry["table"],
            }
            for entry in sorted(
                self.visible_rows.values(),
                key=lambda item: (item["table"], item["key"]),
            )
        ]

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "rows": [
                {
                    "key": entry["key"],
                    "row": entry["row"],
                    "table": entry["table"],
                }
                for entry in self.row_entries()
            ],
        }

    def refresh_digest(self) -> str:
        self.result_digest = digest_json(self.fingerprint_payload())
        return self.result_digest

    def as_json(self) -> dict[str, Any]:
        return {
            "applied_seq": self.applied_seq,
            "generation": self.generation,
            "last_path": list(self.last_path),
            "last_recomputed_segment": self.last_recomputed_segment,
            "last_recomputed_seq": self.last_recomputed_seq,
            "last_trigger": self.last_trigger,
            "logical_clock_ms": self.logical_clock_ms,
            "metrics": self.metrics,
            "recompute_count": self.recompute_count,
            "result_digest": self.result_digest,
            "status": self.status,
            "view_name": self.view_name,
            "visible_rows": self.row_entries(),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "ViewState":
        state = cls(
            view_name=value["view_name"],
            generation=value["generation"],
            status=value["status"],
            applied_seq=value["applied_seq"],
            logical_clock_ms=value["logical_clock_ms"],
            metrics=dict(value["metrics"]),
            result_digest=value["result_digest"],
            recompute_count=value["recompute_count"],
            last_recomputed_seq=value["last_recomputed_seq"],
            last_trigger=value["last_trigger"],
            last_path=list(value["last_path"]),
            last_recomputed_segment=value["last_recomputed_segment"],
        )
        for entry in value["visible_rows"]:
            state.visible_rows[_row_id(entry["table"], entry["key"])] = {
                "key": entry["key"],
                "row": entry["row"],
                "source": entry["source"],
                "table": entry["table"],
            }
        return state


def empty_view_state(spec: ViewSpec) -> ViewState:
    state = ViewState(
        view_name=spec.name,
        generation=spec.generation,
        status=spec.status,
    )
    state.refresh_digest()
    return state


def _row_id(table: str, key: str) -> str:
    return f"{table}\x00{key}"


def empty_base_rows(index: UnifiedIndex) -> dict[str, dict[str, dict[str, Any]]]:
    return {table: {} for table in index.base_schema}


def empty_states(index: UnifiedIndex) -> dict[str, ViewState]:
    return {
        name: empty_view_state(spec)
        for name, spec in index.active_views().items()
    }


def _aggregate_value(value: Any, *, spec: ViewSpec, column: str, function: str) -> Any:
    if value is None:
        return None
    if function in {"sum", "sum_sq"} and (
        isinstance(value, bool) or not isinstance(value, (int, float))
    ):
        raise fail(
            "E_LOG_ROW_TYPE",
            "aggregate source value is not numeric",
            view=spec.name,
            column=column,
            value=value,
        )
    return value


def compute_metrics(spec: ViewSpec, state: ViewState) -> dict[str, Any]:
    """Recompute in fixed row and function order; no NumPy reductions are used."""

    entries = sorted(
        state.visible_rows.values(),
        key=lambda item: (item["table"], item["key"]),
    )
    rules = {rule.function: rule for rule in spec.aggregate_rules}
    metrics: dict[str, Any] = {}

    count_rule = rules["count"]
    if count_rule.column == "*":
        metrics["count"] = len(entries)
    else:
        metrics["count"] = sum(
            1
            for entry in entries
            if entry["row"].get(count_rule.column) is not None
        )
    for function in ("sum", "sum_sq"):
        rule = rules[function]
        total = 0.0
        for entry in entries:
            value = _aggregate_value(
                entry["row"].get(rule.column),
                spec=spec,
                column=rule.column,
                function=function,
            )
            if value is None:
                continue
            numeric = float(value)
            total = total + (numeric if function == "sum" else numeric * numeric)
        metrics[function] = total
    return metrics


def _recompute_visible(spec: ViewSpec, state: ViewState, *, event: ChangeEvent) -> bool:
    parsed_filter = FilterExpression(spec.filter_text, error_code="E_VIEW_FILTER")
    row_key = _row_id(event.base_table, event.key)
    old_entry = state.visible_rows.pop(row_key, None)
    new_entry: dict[str, Any] | None = None
    if event.after is not None and parsed_filter.evaluate(event.after):
        new_entry = {
            "key": event.key,
            "row": event.after,
            "source": {
                "line_number": event.line_number,
                "seq": event.seq,
                "segment_file": event.segment_file,
                "segment_id": event.segment_id,
            },
            "table": event.base_table,
        }
        state.visible_rows[row_key] = new_entry
    return old_entry != new_entry


def _refresh_metrics(spec: ViewSpec, state: ViewState) -> bool:
    previous = state.result_digest
    state.metrics = compute_metrics(spec, state)
    state.refresh_digest()
    return state.result_digest != previous


def recompute_from_base_rows(
    spec: ViewSpec,
    base_rows: dict[str, dict[str, dict[str, Any]]],
    *,
    applied_seq: int,
    logical_clock_ms: int,
    trigger: str,
    path: list[str],
    segment_id: str = "",
    increment_recompute: bool = True,
) -> ViewState:
    state = empty_view_state(spec)
    state.applied_seq = applied_seq
    state.logical_clock_ms = logical_clock_ms
    parsed_filter = FilterExpression(spec.filter_text, error_code="E_VIEW_FILTER")
    for table in spec.base_tables:
        for key in sorted(base_rows.get(table, {})):
            base = base_rows[table][key]
            row = base["row"]
            if not parsed_filter.evaluate(row):
                continue
            state.visible_rows[_row_id(table, key)] = {
                "key": key,
                "row": row,
                "source": dict(base["source"]),
                "table": table,
            }
    state.metrics = compute_metrics(spec, state)
    state.refresh_digest()
    if increment_recompute:
        state.recompute_count = 1
        state.last_recomputed_seq = applied_seq
        state.last_recomputed_segment = segment_id
        state.last_trigger = trigger
        state.last_path = list(path)
    return state


def validate_base_mirror(
    *,
    base_rows: dict[str, dict[str, dict[str, Any]]],
    event: ChangeEvent,
) -> None:
    current_entry = base_rows.setdefault(event.base_table, {}).get(event.key)
    current = None if current_entry is None else current_entry["row"]
    if event.op == "insert" and current is not None:
        raise fail(
            "E_LOG_STATE_MISMATCH",
            "insert baseline is non-empty",
            seq=event.seq,
            segment_id=event.segment_id,
            table=event.base_table,
            key=event.key,
        )
    if event.op in {"update", "delete"} and current != event.before:
        raise fail(
            "E_LOG_STATE_MISMATCH",
            "change before-image does not match the materialized base state",
            seq=event.seq,
            segment_id=event.segment_id,
            table=event.base_table,
            key=event.key,
            expected=current,
            actual=event.before,
        )


def apply_event(
    *,
    index: UnifiedIndex,
    specs: dict[str, ViewSpec],
    topo_order: list[str],
    base_rows: dict[str, dict[str, dict[str, Any]]],
    states: dict[str, ViewState],
    event: ChangeEvent,
) -> dict[str, Any]:
    """Apply one event to in-memory state and return a deterministic decision record."""

    validate_base_mirror(base_rows=base_rows, event=event)
    table_rows = base_rows.setdefault(event.base_table, {})
    if event.op == "delete":
        table_rows.pop(event.key, None)
    elif event.after is not None:
        table_rows[event.key] = {
            "row": event.after,
            "source": {
                "line_number": event.line_number,
                "seq": event.seq,
                "segment_file": event.segment_file,
                "segment_id": event.segment_id,
            },
        }

    decisions: list[dict[str, Any]] = []
    changed: dict[str, list[str]] = {}
    trigger = event.trigger_source
    for name in topo_order:
        spec = specs[name]
        state = states[name]
        direct = event.base_table in spec.base_tables
        upstream_paths = [
            changed[upstream] + [f"view:{name}"]
            for upstream in spec.upstream_views
            if upstream in changed
        ]
        touched = False
        if direct:
            touched = _recompute_visible(spec, state, event=event)
        if touched or upstream_paths:
            path = (
                sorted(upstream_paths)[0]
                if upstream_paths
                else [trigger, f"view:{name}"]
            )
            previous_digest = state.result_digest
            state.metrics = compute_metrics(spec, state)
            state.refresh_digest()
            result_changed = state.result_digest != previous_digest
            action = "recomputed"
            state.recompute_count += 1
            state.last_recomputed_seq = event.seq
            state.last_recomputed_segment = event.segment_id
            state.last_trigger = trigger
            state.last_path = path
        elif direct:
            action = "reused_unchanged"
            path = [trigger, f"view:{name}"]
            result_changed = False
        else:
            action = "skipped_unaffected"
            path = []
            result_changed = False
        state.applied_seq = event.seq
        state.logical_clock_ms = event.logical_clock_ms
        if result_changed:
            changed[name] = path
        decisions.append(
            {
                "action": action,
                "path": path,
                "result_changed": result_changed,
                "trigger": trigger,
                "view_name": name,
            }
        )
    return {
        "decisions": decisions,
        "seq": event.seq,
        "source": {
            "line_number": event.line_number,
            "segment_file": event.segment_file,
            "segment_id": event.segment_id,
        },
    }


def replay(
    *,
    index: UnifiedIndex,
    events: Iterable[ChangeEvent],
    through_seq: int | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, ViewState], list[dict[str, Any]]]:
    specs = index.active_views()
    order = index.topo_order()
    base_rows = empty_base_rows(index)
    states = empty_states(index)
    audit: list[dict[str, Any]] = []
    for event in events:
        if through_seq is not None and event.seq > through_seq:
            break
        audit.append(
            apply_event(
                index=index,
                specs=specs,
                topo_order=order,
                base_rows=base_rows,
                states=states,
                event=event,
            )
        )
    return base_rows, states, audit


def state_mismatch(
    *,
    expected: dict[str, ViewState],
    actual: dict[str, ViewState],
    event_by_seq: dict[int, ChangeEvent],
) -> dict[str, Any] | None:
    for name in sorted(set(expected) | set(actual)):
        if name not in actual:
            source_state = expected[name]
            event = event_by_seq.get(source_state.last_recomputed_seq)
            return {
                "actual": None,
                "expected": "present",
                "field": "view",
                "source": _event_source(event),
                "view_name": name,
            }
        if name not in expected:
            return {
                "actual": "present",
                "expected": None,
                "field": "view",
                "source": None,
                "view_name": name,
            }
        left = expected[name]
        right = actual[name]
        if canonical_json(left.metrics) != canonical_json(right.metrics):
            event = event_by_seq.get(left.last_recomputed_seq)
            return {
                "actual": right.metrics,
                "expected": left.metrics,
                "field": "metrics",
                "source": _event_source(event),
                "view_name": name,
            }
        left_rows = left.row_entries()
        right_rows = right.row_entries()
        if canonical_json(left_rows) != canonical_json(right_rows):
            left_by_id = {
                (entry["table"], entry["key"]): entry for entry in left_rows
            }
            right_by_id = {
                (entry["table"], entry["key"]): entry for entry in right_rows
            }
            for row_id in sorted(set(left_by_id) | set(right_by_id)):
                if left_by_id.get(row_id) != right_by_id.get(row_id):
                    expected_entry = left_by_id.get(row_id)
                    source = None
                    if expected_entry is not None:
                        source = expected_entry["source"]
                    return {
                        "actual": right_by_id.get(row_id),
                        "expected": expected_entry,
                        "field": "visible_row",
                        "row_id": list(row_id),
                        "source": source,
                        "view_name": name,
                    }
        if left.result_digest != right.result_digest:
            event = event_by_seq.get(left.last_recomputed_seq)
            return {
                "actual": right.result_digest,
                "expected": left.result_digest,
                "field": "result_digest",
                "source": _event_source(event),
                "view_name": name,
            }
    return None


def _event_source(event: ChangeEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {
        "line_number": event.line_number,
        "seq": event.seq,
        "segment_file": event.segment_file,
        "segment_id": event.segment_id,
    }

