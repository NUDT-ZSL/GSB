from __future__ import annotations

from audit_forge.engine import AuditEngine
from audit_forge.models import Event
from audit_forge.rules import default_rule_config


def _engine(events: list[dict]) -> AuditEngine:
    engine = AuditEngine(default_rule_config())
    for index, value in enumerate(events, start=1):
        engine.ingest_event(Event.from_dict(value), "causal.jsonl", index)
    return engine


def test_causal_error_codes_and_subjects() -> None:
    engine = _engine(
        [
            {
                "event_id": "orphan",
                "trace_id": "t1",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 1,
                "seq": 2,
                "parent_event_id": "missing",
            },
            {
                "event_id": "self",
                "trace_id": "t1",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 2,
                "seq": 3,
                "parent_event_id": "self",
            },
            {
                "event_id": "cross-child",
                "trace_id": "t2",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 3,
                "seq": 1,
                "parent_event_id": "cross-parent",
            },
            {
                "event_id": "cross-parent",
                "trace_id": "t3",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 4,
                "seq": 1,
                "parent_event_id": None,
            },
            {
                "event_id": "cycle-a",
                "trace_id": "t4",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 5,
                "seq": 1,
                "parent_event_id": "cycle-b",
            },
            {
                "event_id": "cycle-b",
                "trace_id": "t4",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 6,
                "seq": 2,
                "parent_event_id": "cycle-a",
            },
        ]
    )

    anomalies = engine.analyze()["causal_anomalies"]
    by_code = {anomaly["code"]: anomaly for anomaly in anomalies}
    assert by_code["E_ORPHAN"]["event_id"] == "orphan"
    assert by_code["E_ORPHAN"]["trace_id"] == "t1"
    assert by_code["E_ORPHAN"]["parent_event_id"] == "missing"
    assert by_code["E_CROSS_TRACE"]["event_id"] == "cross-child"
    assert by_code["E_CROSS_TRACE"]["parent_trace_id"] == "t3"
    self_loop = next(
        anomaly
        for anomaly in anomalies
        if anomaly["code"] == "E_CYCLE" and anomaly["kind"] == "self_loop"
    )
    assert self_loop["event_id"] == "self"
    cycle = next(
        anomaly
        for anomaly in anomalies
        if anomaly["code"] == "E_CYCLE" and anomaly["kind"] == "cycle"
    )
    assert cycle["event_ids"] == ["cycle-a", "cycle-b"]


def test_seq_duplicate_and_gap_are_reported() -> None:
    engine = _engine(
        [
            {
                "event_id": "a1",
                "trace_id": "t",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 1,
                "seq": 1,
                "parent_event_id": None,
            },
            {
                "event_id": "a3",
                "trace_id": "t",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 2,
                "seq": 3,
                "parent_event_id": "a1",
            },
            {
                "event_id": "a3b",
                "trace_id": "t",
                "actor": "b",
                "action": "x",
                "object": "o",
                "ts": 3,
                "seq": 3,
                "parent_event_id": None,
            },
        ]
    )
    anomalies = engine.seq_anomalies()
    assert any(
        anomaly["code"] == "E_SEQ_GAP"
        and anomaly["missing_ranges"] == [{"start": 2, "end": 2}]
        for anomaly in anomalies
    )
    duplicate = next(
        anomaly
        for anomaly in anomalies
        if anomaly["code"] == "E_SEQ_REGRESSION"
        and anomaly["kind"] == "duplicate_seq"
    )
    assert duplicate["event_ids"] == ["a3", "a3b"]


def test_trace_analysis_rebuilds_ancestry() -> None:
    engine = _engine(
        [
            {
                "event_id": "root",
                "trace_id": "t",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 1,
                "seq": 1,
                "parent_event_id": None,
            },
            {
                "event_id": "child",
                "trace_id": "t",
                "actor": "a",
                "action": "x",
                "object": "o",
                "ts": 2,
                "seq": 2,
                "parent_event_id": "root",
            },
        ]
    )
    trace = engine.analyze()["trace_analyses"][0]
    rows = {row["event_id"]: row for row in trace["events"]}
    assert rows["root"]["ancestry"] == ["root"]
    assert rows["child"]["ancestry"] == ["root", "child"]
    assert trace["chains"] == [
        {"root_event_id": "root", "event_ids": ["root", "child"]}
    ]

