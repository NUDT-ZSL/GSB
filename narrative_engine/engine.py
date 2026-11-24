from __future__ import annotations

from typing import Any, Callable

import numpy as np

from .errors import ChoiceError, Interrupted
from .schema import ChoiceSpec, EventSpec, Script
from .world import FloatTrace, WorldState, apply_effect, conditions_pass


EventCallback = Callable[[dict[str, Any], "ReplayEngine"], None]


class StopController:
    def __init__(self, stop_after_events: int | None = None) -> None:
        self.stop_after_events = stop_after_events
        self.requested = False
        self.signum: int | None = None
        self.reason: str | None = None

    def request(self, signum: int | None = None, reason: str = "signal") -> None:
        self.requested = True
        self.signum = signum
        self.reason = reason

    def note_saved_event(self, event_seq: int) -> None:
        if self.stop_after_events is not None and event_seq >= self.stop_after_events:
            self.request(reason="stop_after_events")


class ReplayEngine:
    def __init__(
        self,
        script: Script,
        *,
        state: WorldState | None = None,
        float_trace: FloatTrace | None = None,
        current_node_id: str | None = None,
        choice_index: int = 0,
        event_records: list[dict[str, Any]] | None = None,
        choice_records: list[dict[str, Any]] | None = None,
        causal_chain: list[dict[str, Any]] | None = None,
        residual_log: list[dict[str, Any]] | None = None,
        residual_exceedances: list[dict[str, Any]] | None = None,
        first_residual_exceedance: dict[str, Any] | None = None,
        fired_once: set[str] | None = None,
        choice_ids: list[str] | None = None,
        node_sequence: list[str] | None = None,
    ) -> None:
        self.script = script
        self.state = state or WorldState.fresh(script)
        self.float_trace = float_trace or FloatTrace.fresh(script, self.state)
        self.current_node_id = current_node_id or script.start_node
        self.choice_index = choice_index
        self.event_records = event_records or []
        self.choice_records = choice_records or []
        self.causal_chain = causal_chain or []
        self.residual_log = residual_log or []
        self.residual_exceedances = residual_exceedances or []
        self.first_residual_exceedance = first_residual_exceedance
        self.fired_once = fired_once or set()
        self.choice_ids = choice_ids or []
        self.node_sequence = node_sequence or [self.current_node_id]

    @classmethod
    def fresh(cls, script: Script) -> "ReplayEngine":
        return cls(script)

    def clone_for_branch(self) -> "ReplayEngine":
        return ReplayEngine(
            self.script,
            state=self.state.clone(),
            float_trace=self.float_trace.clone(),
            current_node_id=self.current_node_id,
            choice_index=self.choice_index,
            event_records=[dict(record) for record in self.event_records],
            choice_records=[dict(record) for record in self.choice_records],
            causal_chain=[dict(item) for item in self.causal_chain],
            residual_log=[dict(item) for item in self.residual_log],
            residual_exceedances=[dict(item) for item in self.residual_exceedances],
            first_residual_exceedance=dict(self.first_residual_exceedance) if self.first_residual_exceedance else None,
            fired_once=set(self.fired_once),
            choice_ids=list(self.choice_ids),
            node_sequence=list(self.node_sequence),
        )

    @property
    def event_seq(self) -> int:
        return len(self.event_records)

    def snapshot_body(self, run_fingerprint: str, *, status: str = "running") -> dict[str, Any]:
        return {
            "format": "narrative-checkpoint/1",
            "status": status,
            "version_id": self.script.version_id,
            "script_format": self.script.format,
            "script_version": self.script.script_version,
            "run_fingerprint": run_fingerprint,
            "fixed_point_scale": self.script.scale,
            "residual_tolerance": format(self.script.residual_tolerance, "f"),
            "current_node_id": self.current_node_id,
            "choice_index": self.choice_index,
            "event_seq": self.event_seq,
            "fired_once": sorted(self.fired_once),
            "world_state": self.state.exact(),
            "float_values": self.float_trace.snapshot(),
            "event_records": self.event_records,
            "choice_records": self.choice_records,
            "causal_chain": self.causal_chain,
            "residual_log": self.residual_log,
            "residual_exceedances": self.residual_exceedances,
            "first_residual_exceedance": self.first_residual_exceedance,
            "choice_ids": self.choice_ids,
            "node_sequence": self.node_sequence,
        }

    @classmethod
    def restore_from_snapshot(cls, script: Script, body: dict[str, Any]) -> "ReplayEngine":
        exact_state = body["world_state"]
        state = WorldState(
            fixed={key: int(value) for key, value in exact_state["fixed"].items()},
            integers={key: int(value) for key, value in exact_state["integers"].items()},
            booleans={key: bool(value) for key, value in exact_state["booleans"].items()},
        )
        float_values = {
            key: np.float64(value) for key, value in body.get("float_values", {}).items()
        }
        float_trace = FloatTrace(float_values, script.scale)
        return cls(
            script,
            state=state,
            float_trace=float_trace,
            current_node_id=body["current_node_id"],
            choice_index=int(body.get("choice_index", 0)),
            event_records=list(body.get("event_records", [])),
            choice_records=list(body.get("choice_records", [])),
            causal_chain=list(body.get("causal_chain", [])),
            residual_log=list(body.get("residual_log", [])),
            residual_exceedances=list(body.get("residual_exceedances", [])),
            first_residual_exceedance=body.get("first_residual_exceedance"),
            fired_once=set(body.get("fired_once", [])),
            choice_ids=list(body.get("choice_ids", [])),
            node_sequence=list(body.get("node_sequence", [])),
        )

    def ordered_events(self, node_id: str | None = None) -> list[EventSpec]:
        node = self.script.node_map[node_id or self.current_node_id]
        indexed = sorted(enumerate(node.events), key=lambda item: (-item[1].priority, item[0]))
        return [event for _, event in indexed]

    def select_next_event(self) -> tuple[EventSpec, list[dict[str, Any]]] | tuple[None, list[Any]]:
        for event in self.ordered_events():
            if event.once and event.id in self.fired_once:
                continue
            passed, evaluations = conditions_pass(event.conditions, self.state, self.float_trace)
            if passed:
                return event, evaluations
        return None, []

    def execute_event(
        self,
        event: EventSpec,
        condition_evaluations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        next_state = self.state.clone()
        next_float_trace = self.float_trace.clone()
        effect_results: list[dict[str, Any]] = []
        for effect in event.effects:
            effect_results.append(apply_effect(effect, self.script, next_state, next_float_trace))

        self.state = next_state
        self.float_trace = next_float_trace
        seq = self.event_seq + 1

        residuals = self._record_residuals(event, seq)
        changed_results = [result for result in effect_results if result.get("changed")]
        for result in changed_results:
            self.causal_chain.append(
                {
                    "change_seq": len(self.causal_chain) + 1,
                    "source_type": "event",
                    "event_seq": seq,
                    "event_ref": f"event:{seq}",
                    "event_id": event.id,
                    "node_id": event.node_id,
                    "state_key": result["key"],
                    "effect": result,
                    "satisfied_conditions": condition_evaluations,
                    "fixed_judgements": [
                        item for item in condition_evaluations if item.get("base") == "fixed"
                    ],
                    "upstream_choices": list(self.choice_ids),
                    "node_sequence": list(self.node_sequence),
                }
            )

        record = {
            "seq": seq,
            "event_ref": f"event:{seq}",
            "event_id": event.id,
            "node_id": event.node_id,
            "priority": event.priority,
            "once": event.once,
            "condition_evaluations": condition_evaluations,
            "effects": effect_results,
            "fixed_float_residuals": residuals,
            "state_after_exact": self.state.exact(),
            "state_after_digest": self.state.digest(),
            "upstream_choices": list(self.choice_ids),
            "node_sequence": list(self.node_sequence),
        }
        self.event_records.append(record)
        self.fired_once.add(event.id)
        return record

    def _record_residuals(self, event: EventSpec, seq: int) -> list[dict[str, Any]]:
        tolerance = float(self.script.residual_tolerance)
        rows: list[dict[str, Any]] = []
        for key in sorted(k for k, spec in self.script.states.items() if spec.base == "fixed"):
            difference = self.float_trace.difference(key, self.state)
            exceeds = bool(difference > tolerance)
            row = {
                "event_seq": seq,
                "event_ref": f"event:{seq}",
                "event_id": event.id,
                "node_id": event.node_id,
                "state_key": key,
                "fixed_value": self.state.fixed[key],
                "fixed_decimal": self.state.decimal_values(self.script)[key],
                "reference_float64": self.float_trace.snapshot()[key],
                "absolute_difference": difference,
                "residual_tolerance": tolerance,
                "exceeds_tolerance": exceeds,
            }
            rows.append(row)
            self.residual_log.append(dict(row))
            if exceeds:
                self.residual_exceedances.append(dict(row))
                if self.first_residual_exceedance is None:
                    self.first_residual_exceedance = dict(row)
        return rows

    def process_available_events(
        self,
        *,
        on_event: EventCallback | None = None,
        stop_controller: StopController | None = None,
        max_events: int = 100_000,
    ) -> list[dict[str, Any]]:
        executed: list[dict[str, Any]] = []
        while True:
            if stop_controller is not None and stop_controller.requested:
                raise Interrupted()
            if self.event_seq >= max_events:
                from .errors import ResourceLimitError

                raise ResourceLimitError("event limit exceeded", details={"max_events": max_events})
            event, evaluations = self.select_next_event()
            if event is None:
                break
            record = self.execute_event(event, evaluations)  # type: ignore[arg-type]
            executed.append(record)
            if on_event is not None:
                on_event(record, self)
            if stop_controller is not None:
                stop_controller.note_saved_event(record["seq"])
                if stop_controller.requested:
                    raise Interrupted()
        return executed

    def choice_statuses(self) -> list[dict[str, Any]]:
        node = self.script.node_map[self.current_node_id]
        statuses: list[dict[str, Any]] = []
        for choice in node.choices:
            passed, evaluations = conditions_pass(choice.conditions, self.state, self.float_trace)
            statuses.append(
                {
                    "choice_id": choice.id,
                    "target": choice.target,
                    "index": choice.index,
                    "enabled": passed,
                    "condition_evaluations": evaluations,
                    "choice": choice,
                }
            )
        return statuses

    def feasibility_report(self) -> dict[str, Any]:
        event_statuses: list[dict[str, Any]] = []
        triggerable: list[str] = []
        blocked: list[str] = []
        already_fired: list[str] = []
        for event in self.ordered_events():
            if event.once and event.id in self.fired_once:
                already_fired.append(event.id)
                event_statuses.append({"event_id": event.id, "status": "already_fired"})
                continue
            passed, evaluations = conditions_pass(event.conditions, self.state, self.float_trace)
            status = "triggerable" if passed else "blocked"
            if passed:
                triggerable.append(event.id)
            else:
                blocked.append(event.id)
            event_statuses.append(
                {"event_id": event.id, "status": status, "condition_evaluations": evaluations}
            )

        choices = self.choice_statuses()
        return {
            "event_statuses": event_statuses,
            "triggerable_event_ids": triggerable,
            "blocked_event_ids": blocked,
            "already_fired_event_ids": already_fired,
            "enabled_choice_ids": [item["choice_id"] for item in choices if item["enabled"]],
            "disabled_choice_ids": [item["choice_id"] for item in choices if not item["enabled"]],
        }

    def take_choice(self, choice_id: str) -> dict[str, Any]:
        node = self.script.node_map[self.current_node_id]
        choice = next((item for item in node.choices if item.id == choice_id), None)
        if choice is None:
            available = [item.id for item in node.choices]
            raise ChoiceError(
                f"choice {choice_id!r} is not available at node {node.id!r}",
                details={"node_id": node.id, "requested_choice": choice_id, "available_choices": available},
            )
        passed, evaluations = conditions_pass(choice.conditions, self.state, self.float_trace)
        if not passed:
            raise ChoiceError(
                f"choice {choice_id!r} preconditions are not satisfied",
                details={"node_id": node.id, "choice_id": choice_id, "conditions": evaluations},
            )

        next_state = self.state.clone()
        next_float_trace = self.float_trace.clone()
        effect_results = [
            apply_effect(effect, self.script, next_state, next_float_trace) for effect in choice.effects
        ]
        self.state = next_state
        self.float_trace = next_float_trace

        self.choice_index += 1
        self.choice_ids.append(choice.id)
        self.current_node_id = choice.target
        self.node_sequence.append(choice.target)
        record = {
            "choice_seq": self.choice_index,
            "choice_ref": f"choice:{self.choice_index}",
            "choice_id": choice.id,
            "from_node_id": node.id,
            "to_node_id": choice.target,
            "condition_evaluations": evaluations,
            "effects": effect_results,
            "upstream_choices": self.choice_ids[:-1],
            "node_sequence": list(self.node_sequence[:-1]),
        }
        self.choice_records.append(record)
        for result in effect_results:
            if not result.get("changed"):
                continue
            self.causal_chain.append(
                {
                    "change_seq": len(self.causal_chain) + 1,
                    "source_type": "choice",
                    "event_seq": None,
                    "event_ref": None,
                    "choice_seq": self.choice_index,
                    "choice_ref": record["choice_ref"],
                    "choice_id": choice.id,
                    "node_id": node.id,
                    "state_key": result["key"],
                    "effect": result,
                    "satisfied_conditions": evaluations,
                    "upstream_choices": list(self.choice_ids),
                    "node_sequence": list(self.node_sequence),
                }
            )
        return record


def run_choice_sequence(
    script: Script,
    choices: list[str] | dict[str, Any],
    *,
    engine: ReplayEngine | None = None,
    on_event: EventCallback | None = None,
    stop_controller: StopController | None = None,
    max_events: int = 100_000,
) -> ReplayEngine:
    if isinstance(choices, dict):
        choice_ids = list(choices.get("choices", []))
    else:
        choice_ids = list(choices)
    engine = engine or ReplayEngine.fresh(script)

    while True:
        engine.process_available_events(
            on_event=on_event,
            stop_controller=stop_controller,
            max_events=max_events,
        )
        if stop_controller is not None and stop_controller.requested:
            from .errors import Interrupted

            raise Interrupted()
        if engine.choice_index >= len(choice_ids):
            break
        requested = choice_ids[engine.choice_index]
        engine.take_choice(requested)

    return engine
