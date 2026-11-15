from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .errors import EngineError
from .hashing import sha256_canonical
from .model import Point, ReplayEvent, Scenario, UnitSpec
from .planner import PlanResult, Planner

SNAPSHOT_FORMAT = "tactical-snapshot-v1"


@dataclass
class MutableUnitState:
    unit_id: str
    position: Point
    remaining_budget_fixed: int
    arrived: bool
    exhausted: bool
    status: str
    moves: int

    def to_dict(self, scale: Decimal) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "position": list(self.position),
            "remaining_budget_fixed": self.remaining_budget_fixed,
            "remaining_budget": float(Decimal(self.remaining_budget_fixed) / scale),
            "arrived": self.arrived,
            "exhausted": self.exhausted,
            "status": self.status,
            "moves": self.moves,
        }

    def state_dict(self) -> dict[str, object]:
        return {
            "position": list(self.position),
            "remaining_budget_fixed": self.remaining_budget_fixed,
            "arrived": self.arrived,
            "exhausted": self.exhausted,
            "status": self.status,
            "moves": self.moves,
        }


@dataclass(frozen=True)
class ResumeInfo:
    requested: bool
    accepted: bool
    reason: str
    restored_event_index: int

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "accepted": self.accepted,
            "reason": self.reason,
            "restored_event_index": self.restored_event_index,
        }


@dataclass(frozen=True)
class ReplayResult:
    scenario_id: str
    fingerprint: str
    decisions: tuple[dict[str, Any], ...]
    snapshots: tuple[dict[str, Any], ...]
    final_units: dict[str, dict[str, object]]
    resume: ResumeInfo
    digest: str
    summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "version": self.fingerprint,
            "resume": self.resume.to_dict(),
            "summary": self.summary,
            "digest": self.digest,
            "decisions": list(self.decisions),
            "final_units": self.final_units,
            "snapshots": list(self.snapshots),
        }


def _initial_states(scenario: Scenario) -> dict[str, MutableUnitState]:
    return {
        unit.id: MutableUnitState(
            unit_id=unit.id,
            position=unit.start,
            remaining_budget_fixed=unit.budget_fixed,
            arrived=False,
            exhausted=False,
            status="ready",
            moves=0,
        )
        for unit in scenario.units
    }


def _states_state(states: dict[str, MutableUnitState], event_index: int) -> dict[str, object]:
    return {
        "event_index": event_index,
        "units": {
            unit_id: states[unit_id].state_dict()
            for unit_id in sorted(states)
        },
    }


def _state_digest(states: dict[str, MutableUnitState], event_index: int) -> str:
    return sha256_canonical(_states_state(states, event_index))


def _make_snapshot(
    scenario: Scenario,
    states: dict[str, MutableUnitState],
    decisions: list[dict[str, Any]],
    event_index: int,
) -> dict[str, Any]:
    return {
        "format": SNAPSHOT_FORMAT,
        "version": scenario.fingerprint,
        "scenario_id": scenario.scenario_id,
        "event_index": event_index,
        "state_digest": _state_digest(states, event_index),
        "decisions_digest": sha256_canonical(decisions),
        "units": {
            unit_id: states[unit_id].to_dict(scenario.precision.scale)
            for unit_id in sorted(states)
        },
        "decisions": decisions.copy(),
    }


def _restore_snapshot(
    scenario: Scenario,
    snapshot: dict[str, Any] | None,
) -> tuple[
    dict[str, MutableUnitState] | None,
    list[dict[str, Any]] | None,
    int,
    ResumeInfo,
]:
    if snapshot is None:
        return None, None, 0, ResumeInfo(False, False, "no snapshot supplied", 0)
    if not isinstance(snapshot, dict):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_FORMAT", 0)
    if snapshot.get("format") != SNAPSHOT_FORMAT:
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_FORMAT", 0)
    if snapshot.get("version") != scenario.fingerprint:
        return (
            None,
            None,
            0,
            ResumeInfo(True, False, "E_SNAPSHOT_VERSION_MISMATCH", 0),
        )
    event_index_raw = snapshot.get("event_index")
    if (
        isinstance(event_index_raw, bool)
        or not isinstance(event_index_raw, int)
        or not (0 <= event_index_raw <= len(scenario.replay))
    ):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_INDEX", 0)
    units_raw = snapshot.get("units")
    if not isinstance(units_raw, dict):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_FORMAT", 0)
    expected_ids = {unit.id for unit in scenario.units}
    if set(units_raw) != expected_ids:
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_UNITS", 0)

    states: dict[str, MutableUnitState] = {}
    try:
        for unit in scenario.units:
            value = units_raw[unit.id]
            if not isinstance(value, dict):
                raise ValueError("unit state is not an object")
            position_raw = value["position"]
            if (
                not isinstance(position_raw, list)
                or len(position_raw) != 2
                or any(isinstance(item, bool) or not isinstance(item, int) for item in position_raw)
            ):
                raise ValueError("invalid unit position")
            position = (position_raw[0], position_raw[1])
            remaining = value["remaining_budget_fixed"]
            arrived = value["arrived"]
            exhausted = value["exhausted"]
            status = value["status"]
            moves = value["moves"]
            if (
                isinstance(remaining, bool)
                or not isinstance(remaining, int)
                or not isinstance(arrived, bool)
                or not isinstance(exhausted, bool)
                or not isinstance(status, str)
                or isinstance(moves, bool)
                or not isinstance(moves, int)
            ):
                raise ValueError("invalid unit state field types")
            states[unit.id] = MutableUnitState(
                unit_id=unit.id,
                position=position,
                remaining_budget_fixed=remaining,
                arrived=arrived,
                exhausted=exhausted,
                status=status,
                moves=moves,
            )
    except (KeyError, TypeError, ValueError):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_FORMAT", 0)

    if snapshot.get("state_digest") != _state_digest(states, event_index_raw):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_STATE_MISMATCH", 0)
    decisions_raw = snapshot.get("decisions")
    if not isinstance(decisions_raw, list) or len(decisions_raw) != event_index_raw:
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_DECISIONS", 0)
    if not all(isinstance(decision, dict) for decision in decisions_raw):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_DECISIONS", 0)
    if snapshot.get("decisions_digest") != sha256_canonical(decisions_raw):
        return None, None, 0, ResumeInfo(True, False, "E_SNAPSHOT_DECISIONS", 0)
    return (
        states,
        decisions_raw,
        event_index_raw,
        ResumeInfo(True, True, "snapshot accepted", event_index_raw),
    )


def _plan_target(event: ReplayEvent) -> Point:
    target = event.data.get("to")
    if not isinstance(target, tuple):
        raise EngineError(
            "E_REPLAY_TARGET",
            "replay event target could not be resolved",
            {"index": event.index, "unit_id": event.unit_id},
        )
    return target


def _plan_decision(
    event: ReplayEvent,
    state: MutableUnitState,
    plan: PlanResult,
) -> dict[str, Any]:
    return {
        "index": event.index,
        "event": event.kind,
        "unit_id": event.unit_id,
        "decision": "planned",
        "from": list(state.position),
        "to": list(_plan_target(event)),
        "plan": plan.to_dict(),
    }


def _apply_event(
    scenario: Scenario,
    planner: Planner,
    event: ReplayEvent,
    states: dict[str, MutableUnitState],
) -> dict[str, Any]:
    state = states[event.unit_id]
    if event.kind in {"move", "plan"}:
        target = _plan_target(event)
        plan = planner.plan(
            state.position,
            target,
            state.remaining_budget_fixed,
            unit_id=event.unit_id,
        )
        decision = _plan_decision(event, state, plan)
        if event.kind == "plan":
            return decision
        if not plan.cost.steps:
            decision["decision"] = "move_rejected"
            decision["reason"] = "no passable path"
            state.status = "blocked"
            return decision
        if not plan.budget.reachable:
            decision["decision"] = "move_rejected"
            decision["reason"] = plan.budget.reason
            state.status = "insufficient_budget"
            return decision
        before = state.remaining_budget_fixed
        state.position = target
        state.remaining_budget_fixed -= plan.cost.fixed_total
        state.moves += 1
        state.status = "moving"
        if state.remaining_budget_fixed < 0:
            state.exhausted = True
            state.status = "overdrawn_at_tolerance"
        decision["decision"] = "move_applied"
        decision["reason"] = "path accepted by fixed-point budget decision"
        decision["remaining_budget_fixed_before"] = before
        decision["remaining_budget_fixed_after"] = state.remaining_budget_fixed
        return decision

    if event.kind == "arrive":
        expected = event.data.get("at", scenario.unit(event.unit_id).goal)
        decision = {
            "index": event.index,
            "event": event.kind,
            "unit_id": event.unit_id,
            "from": list(state.position),
            "expected": list(expected),
        }
        if state.position == expected:
            state.arrived = True
            state.status = "arrived"
            decision["decision"] = "arrived"
            decision["reason"] = "unit is at the declared arrival cell"
        else:
            state.status = "arrival_rejected"
            decision["decision"] = "arrival_rejected"
            decision["reason"] = "unit is not at the declared arrival cell"
        return decision

    if event.kind == "budget_exhausted":
        state.exhausted = True
        state.status = "budget_exhausted"
        return {
            "index": event.index,
            "event": event.kind,
            "unit_id": event.unit_id,
            "from": list(state.position),
            "decision": "budget_exhausted_marked",
            "reason": "decision script marked the unit as out of movement budget",
            "remaining_budget_fixed": state.remaining_budget_fixed,
        }

    return {
        "index": event.index,
        "event": event.kind,
        "unit_id": event.unit_id,
        "from": list(state.position),
        "decision": "waited",
        "reason": "wait event applied",
    }


def replay_scenario(
    scenario: Scenario,
    snapshot: dict[str, Any] | None = None,
) -> ReplayResult:
    restored, restored_decisions, start_index, resume = _restore_snapshot(
        scenario,
        snapshot,
    )
    if restored is not None and restored_decisions is not None:
        states = restored
        decisions = restored_decisions
    else:
        states = _initial_states(scenario)
        decisions = []
        start_index = 0
    planner = Planner(scenario)
    snapshots: list[dict[str, Any]] = [
        _make_snapshot(scenario, states, decisions, start_index)
    ]
    for event in scenario.replay[start_index:]:
        decisions.append(_apply_event(scenario, planner, event, states))
        snapshots.append(
            _make_snapshot(
                scenario,
                states,
                decisions,
                event.index + 1,
            )
        )

    final_units = {
        unit.id: states[unit.id].to_dict(scenario.precision.scale)
        for unit in scenario.units
    }
    applied = sum(decision.get("decision") == "move_applied" for decision in decisions)
    rejected = sum(
        decision.get("decision") in {"move_rejected", "arrival_rejected"}
        for decision in decisions
    )
    arrived = sum(bool(final_units[unit.id]["arrived"]) for unit in scenario.units)
    digest_payload = {
        "scenario_id": scenario.scenario_id,
        "version": scenario.fingerprint,
        "decisions": decisions,
        "final_units": final_units,
    }
    digest = sha256_canonical(digest_payload)
    summary: dict[str, object] = {
        "event_count": len(scenario.replay),
        "decision_count": len(decisions),
        "moves_applied": applied,
        "moves_rejected": rejected,
        "units_arrived": arrived,
        "final_digest": digest,
    }
    return ReplayResult(
        scenario_id=scenario.scenario_id,
        fingerprint=scenario.fingerprint,
        decisions=tuple(decisions),
        snapshots=tuple(snapshots),
        final_units=final_units,
        resume=resume,
        digest=digest,
        summary=summary,
    )
