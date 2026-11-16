"""Replay snapshots from their initial state and compare deterministic traces."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .errors import E_REPLAY, ReplayError
from .models import SchedulerConfig
from .scheduler import Scheduler
from .script import validate_script
from .snapshot import load_snapshot
from .storage import SQLiteStore


def replay_snapshot(
    path: str | Path,
    *,
    lock_wait_budget_ms: int | None = None,
    max_concurrency: int | None = None,
    db_path: str | Path | None = None,
    on_event: Any | None = None,
) -> dict[str, Any]:
    snapshot = load_snapshot(path)
    recorded_config = dict(snapshot["config"])
    effective_config = {
        "lock_wait_budget_ms": (
            lock_wait_budget_ms
            if lock_wait_budget_ms is not None
            else int(recorded_config["lock_wait_budget_ms"])
        ),
        "max_concurrency": (
            max_concurrency
            if max_concurrency is not None
            else int(recorded_config["max_concurrency"])
        ),
        "tick_ms": int(recorded_config.get("tick_ms", 10)),
    }
    try:
        config = SchedulerConfig(**effective_config)
    except ValueError as exc:
        raise ReplayError(f"invalid replay configuration: {exc}") from exc

    replay_temp: tempfile.TemporaryDirectory[str] | None = None
    if db_path is None:
        replay_temp = tempfile.TemporaryDirectory(prefix="txlab-replay-")
        replay_db_path: str | Path = Path(replay_temp.name) / "replay.sqlite3"
    else:
        replay_db_path = db_path

    try:
        with SQLiteStore(replay_db_path) as store:
            initial_state = snapshot["initial_state"]
            store.replace_state(
                initial_state["rows"],
                int(initial_state["data_version"]),
            )
            try:
                script = validate_script(snapshot["script"], store.row_keys())
            except Exception as exc:
                raise ReplayError(
                    "snapshot contains an invalid replay script",
                    details={"cause": str(exc)},
                ) from exc

            scheduler = Scheduler(script, store, config, on_event=on_event)
            replayed = scheduler.run()
    finally:
        if replay_temp is not None:
            replay_temp.cleanup()

    comparison = compare_execution(snapshot["execution"], replayed)
    report: dict[str, Any] = {
        "code": E_REPLAY if not comparison["reproducible"] else "OK",
        "ok": comparison["reproducible"],
        "snapshot_name": snapshot["name"],
        "snapshot_path": str(path),
        "snapshot_config": recorded_config,
        "replay_config": config.to_dict(),
        "parameter_mismatch": recorded_config != config.to_dict(),
        "diverged_sessions": comparison["diverged_sessions"],
        "outcome_comparison": comparison["outcome_comparison"],
        "event_comparison": comparison["event_comparison"],
        "replayed_summary": replayed["summary"],
    }
    return report


def compare_execution(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    expected_sessions = list(expected.get("sessions", []))
    actual_sessions = list(actual.get("sessions", []))
    expected_by_id = {
        session["session_id"]: session for session in expected_sessions
    }
    actual_by_id = {session["session_id"]: session for session in actual_sessions}
    session_order = [session["session_id"] for session in expected_sessions]
    for session_id in sorted(actual_by_id):
        if session_id not in expected_by_id:
            session_order.append(session_id)

    diverged: list[str] = []
    outcome_comparison: list[dict[str, Any]] = []
    expected_events = _normalize_events(expected.get("events", []))
    actual_events = _normalize_events(actual.get("events", []))

    for session_id in session_order:
        expected_session = expected_by_id.get(session_id)
        actual_session = actual_by_id.get(session_id)
        expected_outcome = _outcome_tuple(expected_session)
        actual_outcome = _outcome_tuple(actual_session)
        expected_session_events = [
            event for event in expected_events if event.get("session_id") == session_id
        ]
        actual_session_events = [
            event for event in actual_events if event.get("session_id") == session_id
        ]
        events_equal = expected_session_events == actual_session_events
        outcomes_equal = expected_outcome == actual_outcome
        if not events_equal or not outcomes_equal:
            diverged.append(session_id)
        outcome_comparison.append(
            {
                "session_id": session_id,
                "expected_outcome": expected_outcome[0],
                "expected_code": expected_outcome[1],
                "actual_outcome": actual_outcome[0],
                "actual_code": actual_outcome[1],
                "outcome_equal": outcomes_equal,
                "session_events_equal": events_equal,
            }
        )

    sequence_equal = expected_events == actual_events
    first_divergence = _first_event_divergence(expected_events, actual_events)
    if not sequence_equal and not diverged and first_divergence is not None:
        divergent_references = first_divergence.get("session_ids", [])
        diverged.extend(divergent_references)
    if not sequence_equal and not diverged:
        diverged.extend(session_order)

    order_index = {session_id: index for index, session_id in enumerate(session_order)}
    diverged = sorted(set(diverged), key=lambda sid: (order_index.get(sid, 10**9), sid))
    reproducible = sequence_equal and not any(
        not item["outcome_equal"] or not item["session_events_equal"]
        for item in outcome_comparison
    )

    return {
        "reproducible": reproducible,
        "diverged_sessions": diverged,
        "outcome_comparison": outcome_comparison,
        "event_comparison": {
            "equal": sequence_equal,
            "expected_event_count": len(expected_events),
            "actual_event_count": len(actual_events),
            "first_divergence": first_divergence,
        },
    }


def _outcome_tuple(session: dict[str, Any] | None) -> tuple[Any, Any]:
    if session is None:
        return (None, None)
    return (session.get("outcome"), session.get("code"))


def _normalize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for event in events:
        copy = dict(event)
        copy.pop("timestamp", None)
        normalized.append(copy)
    return normalized


def _first_event_divergence(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any] | None:
    limit = max(len(expected), len(actual))
    for index in range(limit):
        expected_event = expected[index] if index < len(expected) else None
        actual_event = actual[index] if index < len(actual) else None
        if expected_event == actual_event:
            continue
        session_ids = sorted(
            {
                item.get("session_id")
                for item in (expected_event, actual_event)
                if item is not None and item.get("session_id") is not None
            }
        )
        return {
            "index": index,
            "expected": expected_event,
            "actual": actual_event,
            "session_ids": session_ids,
        }
    return None

