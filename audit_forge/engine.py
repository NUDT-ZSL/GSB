from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from .causality import analyze_all_traces, group_by_trace
from .models import Event
from .rules import RuleConfig, build_cases
from .util import canonical_bytes
from .validation import normalize_line, validate_event_mapping


def _empty_stats() -> dict[str, int]:
    return {
        "lines_read": 0,
        "valid_lines": 0,
        "accepted_deliveries": 0,
        "unique_events": 0,
        "rejected_lines": 0,
        "duplicate_deliveries": 0,
        "conflicts": 0,
    }


def _dedupe_key(value: dict[str, Any]) -> str:
    return canonical_bytes(value).decode("utf-8")


def _missing_ranges(sequences: list[int]) -> list[dict[str, int]]:
    if not sequences:
        return []
    ranges: list[dict[str, int]] = []
    expected = 1
    for sequence in sorted(set(sequences)):
        if sequence > expected:
            ranges.append({"start": expected, "end": sequence - 1})
        expected = sequence + 1
    return ranges


def _contiguous_watermark(sequences: Iterable[int]) -> int:
    watermark = 0
    for sequence in sorted(set(sequences)):
        if sequence != watermark + 1:
            break
        watermark = sequence
    return watermark


class AuditEngine:
    """In-memory state. Persistence is intentionally handled by Workspace."""

    def __init__(self, rules: RuleConfig, generation: int = 0) -> None:
        self.rules = rules
        self.generation = generation
        self.events: dict[str, Event] = {}
        self.origins: dict[str, dict[str, Any]] = {}
        self.rejects: dict[str, dict[str, Any]] = {}
        self.ingest_anomalies: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, int] = _empty_stats()
        self.load_issues: list[dict[str, Any]] = []
        self.cases: list[dict[str, Any]] = []
        self.cases_snapshot_valid = True

    @property
    def max_seq_watermarks(self) -> dict[str, int]:
        watermarks: dict[str, int] = {}
        for event in self.events.values():
            watermarks[event.trace_id] = max(
                watermarks.get(event.trace_id, 0), event.seq
            )
        return dict(sorted(watermarks.items()))

    @property
    def contiguous_seq_watermarks(self) -> dict[str, int]:
        sequences: dict[str, list[int]] = defaultdict(list)
        for event in self.events.values():
            sequences[event.trace_id].append(event.seq)
        return {
            trace_id: _contiguous_watermark(values)
            for trace_id, values in sorted(sequences.items())
        }

    def _add_reject(self, record: dict[str, Any]) -> None:
        self.rejects[_dedupe_key(record)] = record
        self.stats["rejected_lines"] += 1

    def _add_ingest_anomaly(self, record: dict[str, Any]) -> None:
        self.ingest_anomalies[_dedupe_key(record)] = record
        if record["code"] == "E_DUPLICATE_EVENT":
            self.stats["duplicate_deliveries"] += 1
        if record["code"] == "E_EVENT_CONFLICT":
            self.stats["conflicts"] += 1

    def ingest_event(
        self,
        event: Event,
        source: str,
        line_no: int,
    ) -> bool:
        existing = self.events.get(event.event_id)
        if existing is not None:
            if existing == event:
                first = self.origins.get(event.event_id, {})
                self._add_ingest_anomaly(
                    {
                        "code": "E_DUPLICATE_EVENT",
                        "severity": "warning",
                        "message": "exact duplicate delivery; first event retained",
                        "event_id": event.event_id,
                        "trace_id": event.trace_id,
                        "source": source,
                        "line": line_no,
                        "first_source": first.get("source"),
                        "first_line": first.get("line"),
                    }
                )
            else:
                fields = sorted(
                    field
                    for field in existing.as_dict()
                    if existing.as_dict()[field] != event.as_dict()[field]
                )
                self._add_ingest_anomaly(
                    {
                        "code": "E_EVENT_CONFLICT",
                        "severity": "error",
                        "message": "event_id was delivered with conflicting content; first event retained",
                        "event_id": event.event_id,
                        "trace_id": existing.trace_id,
                        "source": source,
                        "line": line_no,
                        "conflicting_fields": fields,
                        "retained_trace_id": existing.trace_id,
                        "rejected_trace_id": event.trace_id,
                    }
                )
            return False

        previous_watermark = self.max_seq_watermarks.get(event.trace_id, 0)
        if event.seq <= previous_watermark:
            self._add_ingest_anomaly(
                {
                    "code": "E_SEQ_REGRESSION",
                    "severity": "warning",
                    "kind": "arrival_regression",
                    "message": "event arrived below the existing seq watermark; accepted for deterministic reordering",
                    "event_id": event.event_id,
                    "trace_id": event.trace_id,
                    "seq": event.seq,
                    "previous_watermark": previous_watermark,
                    "source": source,
                    "line": line_no,
                }
            )

        self.events[event.event_id] = event
        self.origins[event.event_id] = {"source": source, "line": line_no}
        self.stats["accepted_deliveries"] += 1
        self.stats["unique_events"] = len(self.events)
        return True

    def ingest_stream(
        self,
        stream: BinaryIO,
        source: str,
        checkpoint_every: int = 0,
        checkpoint_callback: Any = None,
    ) -> dict[str, int]:
        before = len(self.rejects), len(self.ingest_anomalies)
        pending = 0
        current_rejects = 0
        current_anomalies = 0
        for line_no, raw_line in enumerate(stream, start=1):
            self.stats["lines_read"] += 1
            event, reject = normalize_line(raw_line, line_no, source)
            if reject is not None:
                self._add_reject(reject)
                current_rejects += 1
            else:
                self.stats["valid_lines"] += 1
                event_count_before = len(self.ingest_anomalies)
                self.ingest_event(event, source, line_no)
                if len(self.ingest_anomalies) > event_count_before:
                    current_anomalies += 1
                pending += 1
                if (
                    checkpoint_every > 0
                    and pending >= checkpoint_every
                    and checkpoint_callback is not None
                ):
                    checkpoint_callback(self)
                    pending = 0
        return {
            "new_rejects": current_rejects,
            "new_ingest_anomalies": current_anomalies,
            "reject_count_before": before[0],
            "anomaly_count_before": before[1],
            "pending_since_checkpoint": pending,
        }

    def seq_anomalies(self) -> list[dict[str, Any]]:
        traces = group_by_trace(self.events.values())
        anomalies: list[dict[str, Any]] = []
        for trace_id, events in traces.items():
            by_seq: dict[int, list[Event]] = defaultdict(list)
            for event in events:
                by_seq[event.seq].append(event)
            for sequence, members in sorted(by_seq.items()):
                if len(members) > 1:
                    event_ids = sorted(event.event_id for event in members)
                    anomalies.append(
                        {
                            "code": "E_SEQ_REGRESSION",
                            "severity": "error",
                            "kind": "duplicate_seq",
                            "message": "multiple events share the same seq in one trace",
                            "event_id": event_ids[0],
                            "event_ids": event_ids,
                            "trace_id": trace_id,
                            "seq": sequence,
                        }
                    )
            missing = _missing_ranges([event.seq for event in events])
            if missing:
                first_seq = min(event.seq for event in events)
                first_event = min(
                    (event for event in events if event.seq == first_seq),
                    key=lambda event: event.event_id,
                )
                anomalies.append(
                    {
                        "code": "E_SEQ_GAP",
                        "severity": "error",
                        "message": "seq gaps exist in trace",
                        "event_id": first_event.event_id,
                        "event_ids": [first_event.event_id],
                        "trace_id": trace_id,
                        "missing_ranges": missing,
                    }
                )
        return anomalies

    def analyze(self) -> dict[str, Any]:
        traces, causal_anomalies, _ = analyze_all_traces(self.events.values())
        anomalies = list(self.ingest_anomalies.values())
        anomalies.extend(self.seq_anomalies())
        anomalies.extend(causal_anomalies)
        anomalies.sort(
            key=lambda item: (
                item.get("trace_id", ""),
                item.get("event_id", ""),
                item["code"],
                item.get("kind", ""),
                item.get("line", 0),
            )
        )
        return {
            "trace_analyses": traces,
            "causal_anomalies": causal_anomalies,
            "anomalies": anomalies,
        }

    def recompute_cases(self) -> list[dict[str, Any]]:
        self.cases = build_cases(self.events.values(), self.rules)
        self.cases_snapshot_valid = True
        return self.cases

    def core_state(self) -> dict[str, Any]:
        events = [
            event.as_dict()
            for event in sorted(self.events.values(), key=lambda item: item.event_id)
        ]
        rejects = sorted(
            self.rejects.values(),
            key=lambda item: (
                item.get("source", ""),
                item.get("line", 0),
                item["code"],
                _dedupe_key(item),
            ),
        )
        ingest_anomalies = sorted(
            self.ingest_anomalies.values(),
            key=lambda item: (
                item.get("source", ""),
                item.get("line", 0),
                item["code"],
                item.get("event_id", ""),
                _dedupe_key(item),
            ),
        )
        origins = {
            event_id: self.origins[event_id]
            for event_id in sorted(self.origins)
        }
        stats = dict(self.stats)
        stats["unique_events"] = len(self.events)
        return {
            "events": events,
            "event_origins": origins,
            "rejects": rejects,
            "ingest_anomalies": ingest_anomalies,
            "watermarks": {
                "max_seq": self.max_seq_watermarks,
                "contiguous_seq": self.contiguous_seq_watermarks,
            },
            "stats": stats,
        }

    @classmethod
    def from_checkpoint_core(
        cls,
        core: dict[str, Any],
        rules: RuleConfig,
        generation: int,
    ) -> "AuditEngine":
        engine = cls(rules, generation=generation)
        raw_events = core.get("events")
        if not isinstance(raw_events, list):
            raw_events = []
        for raw_event in raw_events:
            event, reject = validate_event_mapping(
                raw_event,
                source="checkpoint",
            )
            if event is None:
                engine.load_issues.append(
                    {
                        "code": "E_CHECKPOINT_EVENT_SKIPPED",
                        "message": "checkpoint event could not be parsed",
                        "reject": reject,
                    }
                )
                continue
            engine.events[event.event_id] = event
        raw_origins = core.get("event_origins")
        if not isinstance(raw_origins, dict):
            raw_origins = {}
        for event_id, origin in raw_origins.items():
            if isinstance(origin, dict):
                engine.origins[str(event_id)] = origin
        raw_rejects = core.get("rejects")
        if not isinstance(raw_rejects, list):
            raw_rejects = []
        for raw_reject in raw_rejects:
            if isinstance(raw_reject, dict) and "code" in raw_reject:
                engine.rejects[_dedupe_key(raw_reject)] = raw_reject
        raw_anomalies = core.get("ingest_anomalies")
        if not isinstance(raw_anomalies, list):
            raw_anomalies = []
        for raw_anomaly in raw_anomalies:
            if isinstance(raw_anomaly, dict) and "code" in raw_anomaly:
                engine.ingest_anomalies[_dedupe_key(raw_anomaly)] = raw_anomaly
        raw_stats = core.get("stats")
        if isinstance(raw_stats, dict):
            for key in _empty_stats():
                value = raw_stats.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool):
                    engine.stats[key] = value
        engine.stats["unique_events"] = len(engine.events)
        return engine

    @classmethod
    def from_stream(
        cls,
        path: str | Path,
        rules: RuleConfig,
        source: str | None = None,
    ) -> tuple["AuditEngine", dict[str, int]]:
        engine = cls(rules)
        source_name = source or Path(path).name
        with Path(path).open("rb") as stream:
            import_result = engine.ingest_stream(stream, source_name)
        return engine, import_result
