from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Iterator

from .canonical import (
    canonical_json,
    digest_value,
    extend_chain,
)
from .engine import (
    Engine,
    EngineState,
    FrameOutcome,
    build_initial_state,
    initial_ledger,
)
from .errors import (
    E_FINAL_STATE,
    E_INCOMPLETE_RUN,
    E_SCENARIO_MISMATCH,
    E_TRAJECTORY_MISMATCH,
    EngineError,
    StorageError,
    VerificationError,
)
from .scenario import Scenario, parse_scenario
from .storage import SQLiteStore


@dataclass
class Step:
    event: dict[str, Any]
    outcome: FrameOutcome
    ledger_digest: str
    trajectory_digest: str


@dataclass
class Simulation:
    scenario: Scenario
    initial_state: EngineState
    initial_state_digest: str
    initial_ledger_digest: str
    initial_trajectory_digest: str
    steps: list[Step]
    final_state: EngineState
    final_state_digest: str
    final_ledger_digest: str
    final_trajectory_digest: str


def _initial_digests(
    state: EngineState,
) -> tuple[str, str, str]:
    state_digest = state.digest()
    ledger_digest = digest_value(initial_ledger(state))
    trajectory_digest = extend_chain("GENESIS", state_digest, ledger_digest)
    return state_digest, ledger_digest, trajectory_digest


def iter_simulation(scenario: Scenario) -> Iterator[Any]:
    """Yield initial metadata, then each frame step, then final metadata."""
    state = build_initial_state(scenario)
    state_digest, ledger_digest, trajectory_digest = _initial_digests(state)
    yield {
        "kind": "initial",
        "state": state,
        "state_digest": state_digest,
        "ledger_digest": ledger_digest,
        "trajectory_digest": trajectory_digest,
    }

    engine = Engine(scenario, state)
    for event in scenario.events:
        outcome = engine.apply_event(event)
        ledger_digest = extend_chain(
            ledger_digest, canonical_json(outcome.ledger)
        )
        trajectory_digest = extend_chain(
            trajectory_digest,
            outcome.event_digest,
            outcome.state_digest,
            ledger_digest,
        )
        step = Step(
            event=event,
            outcome=outcome,
            ledger_digest=ledger_digest,
            trajectory_digest=trajectory_digest,
        )
        yield {"kind": "frame", "step": step, "state": engine.state}

    engine.finish()
    yield {
        "kind": "final",
        "state": engine.state,
        "state_digest": engine.state.digest(),
        "ledger_digest": ledger_digest,
        "trajectory_digest": trajectory_digest,
    }


def check_expected_states(scenario: Scenario, state: EngineState) -> None:
    mismatch = []
    for declaration in scenario.streams:
        actual = state.streams[declaration.stream_id].state
        if actual != declaration.expected_final_state:
            mismatch.append(
                {
                    "actual": actual,
                    "expected": declaration.expected_final_state,
                    "stream_id": declaration.stream_id,
                }
            )
    if mismatch:
        raise EngineError(
            E_FINAL_STATE,
            "one or more streams did not reach the declared final state",
            {"mismatches": mismatch, "seq": state.committed_seq},
        )


def simulate(scenario: Scenario, check_expected: bool = True) -> Simulation:
    initial_state: EngineState | None = None
    initial_state_digest = ""
    initial_ledger_digest = ""
    initial_trajectory_digest = ""
    final_state: EngineState | None = None
    final_state_digest = ""
    final_ledger_digest = ""
    final_trajectory_digest = ""
    steps: list[Step] = []

    for item in iter_simulation(scenario):
        kind = item["kind"]
        if kind == "initial":
            initial_state = item["state"]
            initial_state_digest = item["state_digest"]
            initial_ledger_digest = item["ledger_digest"]
            initial_trajectory_digest = item["trajectory_digest"]
        elif kind == "frame":
            steps.append(item["step"])
        else:
            final_state = item["state"]
            final_state_digest = item["state_digest"]
            final_ledger_digest = item["ledger_digest"]
            final_trajectory_digest = item["trajectory_digest"]

    assert initial_state is not None and final_state is not None
    if check_expected:
        check_expected_states(scenario, final_state)
    return Simulation(
        scenario=scenario,
        initial_state=initial_state,
        initial_state_digest=initial_state_digest,
        initial_ledger_digest=initial_ledger_digest,
        initial_trajectory_digest=initial_trajectory_digest,
        steps=steps,
        final_state=final_state,
        final_state_digest=final_state_digest,
        final_ledger_digest=final_ledger_digest,
        final_trajectory_digest=final_trajectory_digest,
    )


def preflight(scenario: Scenario) -> Simulation:
    return simulate(scenario, check_expected=True)


def _initial_snapshot_payload(
    run_id: str,
    scenario: Scenario,
    simulation: Simulation,
) -> dict[str, Any]:
    return {
        "at_ms": simulation.initial_state.logical_clock_ms,
        "event_digest": None,
        "event_json": None,
        "ledger_digest": simulation.initial_ledger_digest,
        "ledger_json": canonical_json(initial_ledger(simulation.initial_state)),
        "run_id": run_id,
        "seq": 0,
        "state_digest": simulation.initial_state_digest,
        "state_json": canonical_json(simulation.initial_state.to_dict()),
        "trajectory_digest": simulation.initial_trajectory_digest,
    }


def _frame_from_engine(
    run_id: str, engine: Engine, outcome: FrameOutcome, step_ledger_digest: str,
    step_trajectory_digest: str, event: dict[str, Any]
) -> tuple[dict[str, Any], Step]:
    step = Step(
        event=copy.deepcopy(event),
        outcome=outcome,
        ledger_digest=step_ledger_digest,
        trajectory_digest=step_trajectory_digest,
    )
    payload = {
        "at_ms": outcome.at_ms,
        "event_digest": outcome.event_digest,
        "event_json": canonical_json(event),
        "ledger_digest": step_ledger_digest,
        "ledger_json": canonical_json(outcome.ledger),
        "run_id": run_id,
        "seq": outcome.seq,
        "state_digest": outcome.state_digest,
        "state_json": canonical_json(engine.state.to_dict()),
        "trajectory_digest": step_trajectory_digest,
    }
    return payload, step


def _write_trace_line(handle: Any, record: dict[str, Any]) -> None:
    handle.write(canonical_json(record) + "\n")
    handle.flush()


def run_scenario(
    scenario: Scenario,
    db_path: str,
    run_id: str,
    trace_handle: Any | None = None,
    crash_before_commit: int | None = None,
    crash_after_commit: int | None = None,
    pause_before_commit_ms: int = 0,
    pause_at_seq: int | None = None,
) -> dict[str, Any]:
    simulation = preflight(scenario)
    store = SQLiteStore(db_path)
    try:
        store.open(create=True)
        run = store.load_run(run_id)
        if run is not None:
            raise StorageError(
                "E_RUN_EXISTS",
                f"run {run_id!r} already exists; use resume",
                {"run_id": run_id},
            )

        store.create_run(
            run_id=run_id,
            scenario=scenario,
            initial_snapshot=_initial_snapshot_payload(
                run_id, scenario, simulation
            ),
        )
        engine = Engine(scenario, simulation.initial_state)
        ledger_digest = simulation.initial_ledger_digest
        trajectory_digest = simulation.initial_trajectory_digest

        for event in scenario.events:
            outcome = engine.apply_event(event)
            ledger_digest = extend_chain(
                ledger_digest, canonical_json(outcome.ledger)
            )
            trajectory_digest = extend_chain(
                trajectory_digest,
                outcome.event_digest,
                outcome.state_digest,
                ledger_digest,
            )
            payload, _ = _frame_from_engine(
                run_id,
                engine,
                outcome,
                ledger_digest,
                trajectory_digest,
                event,
            )
            committed = store.commit_frame(
                run_id,
                payload,
                crash_before_commit=crash_before_commit,
                crash_after_commit=crash_after_commit,
                pause_before_commit_ms=pause_before_commit_ms,
                pause_at_seq=pause_at_seq,
            )
            if not committed:
                continue
            if trace_handle is not None:
                _write_trace_line(
                    trace_handle,
                    engine.trace_record(
                        outcome,
                        outcome.state_digest,
                        trajectory_digest,
                        ledger_digest,
                    ),
                )

        engine.finish()
        check_expected_states(scenario, engine.state)
        store.finalize_run(
            run_id,
            engine.state.digest(),
            trajectory_digest,
            ledger_digest,
        )
        return store.status(run_id)
    finally:
        store.close()


def resume_scenario(
    scenario: Scenario,
    db_path: str,
    run_id: str,
    trace_handle: Any | None = None,
    crash_before_commit: int | None = None,
    crash_after_commit: int | None = None,
    pause_before_commit_ms: int = 0,
    pause_at_seq: int | None = None,
) -> dict[str, Any]:
    preflight(scenario)
    store = SQLiteStore(db_path)
    try:
        store.open(create=False)
        run = store.load_run(run_id)
        if run is None:
            raise StorageError(
                "E_RUN_NOT_FOUND",
                f"run {run_id!r} was not found",
                {"run_id": run_id},
            )
        if run["scenario_hash"] != scenario.canonical_hash:
            raise StorageError(
                E_SCENARIO_MISMATCH,
                "scenario hash does not match the persisted run",
                {
                    "persisted": run["scenario_hash"],
                    "requested": scenario.canonical_hash,
                    "run_id": run_id,
                },
            )
        if run["status"] == "complete":
            return store.status(run_id)

        snapshot = store.load_snapshot(run_id, int(run["committed_seq"]))
        if snapshot is None:
            raise StorageError(
                "E_DB_CORRUPT",
                "committed snapshot is missing",
                {"run_id": run_id, "committed_seq": run["committed_seq"]},
            )
        state = EngineState.from_dict(json.loads(snapshot["state_json"]))
        if state.digest() != snapshot["state_digest"]:
            raise StorageError(
                "E_DB_CORRUPT",
                "committed snapshot state digest does not match its payload",
                {"run_id": run_id, "seq": snapshot["seq"]},
            )

        engine = Engine(scenario, state)
        ledger_digest = str(snapshot["ledger_digest"])
        trajectory_digest = str(snapshot["trajectory_digest"])
        committed_seq = int(snapshot["seq"])

        for event in scenario.events:
            seq = int(event["seq"])
            if seq <= committed_seq:
                continue
            outcome = engine.apply_event(event)
            ledger_digest = extend_chain(
                ledger_digest, canonical_json(outcome.ledger)
            )
            trajectory_digest = extend_chain(
                trajectory_digest,
                outcome.event_digest,
                outcome.state_digest,
                ledger_digest,
            )
            payload, _ = _frame_from_engine(
                run_id,
                engine,
                outcome,
                ledger_digest,
                trajectory_digest,
                event,
            )
            committed = store.commit_frame(
                run_id,
                payload,
                crash_before_commit=crash_before_commit,
                crash_after_commit=crash_after_commit,
                pause_before_commit_ms=pause_before_commit_ms,
                pause_at_seq=pause_at_seq,
            )
            if not committed:
                continue
            if trace_handle is not None:
                _write_trace_line(
                    trace_handle,
                    engine.trace_record(
                        outcome,
                        outcome.state_digest,
                        trajectory_digest,
                        ledger_digest,
                    ),
                )

        engine.finish()
        check_expected_states(scenario, engine.state)
        store.finalize_run(
            run_id,
            engine.state.digest(),
            trajectory_digest,
            ledger_digest,
        )
        return store.status(run_id)
    finally:
        store.close()


def verify_run(scenario: Scenario, db_path: str, run_id: str) -> dict[str, Any]:
    simulation = preflight(scenario)
    store = SQLiteStore(db_path)
    try:
        store.open(create=False)
        integrity = store.integrity_check()
        if integrity != "ok":
            raise VerificationError(
                "E_DB_CORRUPT",
                "SQLite integrity_check failed",
                {"integrity_check": integrity},
            )
        run, snapshots = store.read_run_and_snapshots(run_id)
        if run["scenario_hash"] != scenario.canonical_hash:
            raise VerificationError(
                E_SCENARIO_MISMATCH,
                "scenario hash does not match the persisted run",
                {
                    "persisted": run["scenario_hash"],
                    "requested": scenario.canonical_hash,
                },
            )
        if run["status"] != "complete" or int(run["committed_seq"]) != int(
            run["event_count"]
        ):
            raise VerificationError(
                E_INCOMPLETE_RUN,
                "run is not complete; call resume before verify",
                {
                    "committed_seq": run["committed_seq"],
                    "event_count": run["event_count"],
                    "status": run["status"],
                },
            )

        expected_seqs = list(range(0, int(run["event_count"]) + 1))
        actual_seqs = [int(row["seq"]) for row in snapshots]
        if actual_seqs != expected_seqs:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "snapshot sequence contains a gap, half-commit, or extra row",
                {
                    "committed_seq": run["committed_seq"],
                    "first_mismatch": next(
                        (
                            index
                            for index, (left, right) in enumerate(
                                zip(actual_seqs, expected_seqs)
                            )
                            if left != right
                        ),
                        min(len(actual_seqs), len(expected_seqs)),
                    ),
                },
            )

        initial = snapshots[0]
        if json.loads(initial["state_json"]) != json.loads(
            canonical_json(simulation.initial_state.to_dict())
        ):
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "persisted initial state payload does not match fresh simulation",
                {"seq": 0},
            )
        if json.loads(initial["ledger_json"]) != json.loads(
            canonical_json(initial_ledger(simulation.initial_state))
        ):
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "persisted initial ledger payload does not match fresh simulation",
                {"seq": 0},
            )
        if initial["state_digest"] != simulation.initial_state_digest:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "persisted initial state digest does not match fresh simulation",
                {"seq": 0},
            )
        if initial["ledger_digest"] != simulation.initial_ledger_digest:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "persisted initial ledger digest does not match fresh simulation",
                {"seq": 0},
            )
        if initial["trajectory_digest"] != simulation.initial_trajectory_digest:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "persisted initial trajectory digest does not match fresh simulation",
                {"seq": 0},
            )

        engine = Engine(scenario, simulation.initial_state)
        ledger_digest = simulation.initial_ledger_digest
        trajectory_digest = simulation.initial_trajectory_digest
        for index, event in enumerate(scenario.events, start=1):
            outcome = engine.apply_event(event)
            ledger_digest = extend_chain(
                ledger_digest, canonical_json(outcome.ledger)
            )
            trajectory_digest = extend_chain(
                trajectory_digest,
                outcome.event_digest,
                outcome.state_digest,
                ledger_digest,
            )
            row = snapshots[index]
            state_payload = json.loads(row["state_json"])
            expected_state_payload = json.loads(
                canonical_json(engine.state.to_dict())
            )
            checks = {
                "event_digest": (row["event_digest"], outcome.event_digest),
                "event_json": (json.loads(row["event_json"]), event),
                "ledger_digest": (row["ledger_digest"], ledger_digest),
                "state_digest": (row["state_digest"], outcome.state_digest),
                "state_json": (state_payload, expected_state_payload),
                "trajectory_digest": (
                    row["trajectory_digest"],
                    trajectory_digest,
                ),
            }
            for field, (persisted, expected) in checks.items():
                if persisted != expected:
                    raise VerificationError(
                        E_TRAJECTORY_MISMATCH,
                        f"persisted {field} does not match deterministic replay",
                        {"field": field, "seq": index},
                    )

        engine.finish()
        check_expected_states(scenario, engine.state)
        if run["final_state_digest"] != engine.state.digest():
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "run final_state_digest does not match deterministic replay",
                {"seq": run["committed_seq"]},
            )
        if run["final_trajectory_digest"] != trajectory_digest:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "run final trajectory digest does not match deterministic replay",
                {"seq": run["committed_seq"]},
            )
        if run["final_ledger_digest"] != ledger_digest:
            raise VerificationError(
                E_TRAJECTORY_MISMATCH,
                "run final ledger digest does not match deterministic replay",
                {"seq": run["committed_seq"]},
            )

        return {
            "committed_seq": int(run["committed_seq"]),
            "event_count": int(run["event_count"]),
            "expected_final_states_match": True,
            "final_ledger_digest": ledger_digest,
            "final_state_digest": engine.state.digest(),
            "frames_checked": int(run["event_count"]),
            "integrity_check": integrity,
            "ok": True,
            "run_id": run_id,
            "scenario_hash": scenario.canonical_hash,
            "snapshots_checked": len(snapshots),
            "trajectory_digest": trajectory_digest,
        }
    finally:
        store.close()


def load_stored_scenario(run: dict[str, Any]) -> Scenario:
    try:
        raw = json.loads(run["scenario_json"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise StorageError(
            "E_DB_CORRUPT",
            "persisted scenario cannot be decoded",
            {"run_id": run.get("run_id")},
        ) from exc
    return parse_scenario(raw)

