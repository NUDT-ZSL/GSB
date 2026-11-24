from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from .errors import DigestError, DomainError, InputError
from .fixed import (
    compare_fixed,
    compare_float,
    error_decimal,
    fixed_text,
    fixed_to_float,
)
from .models import (
    BoolKey,
    Bundle,
    Condition,
    Effect,
    EventDef,
    FixedKey,
    SceneDef,
)

SIM_FORMAT = "NarrativeWorkbench.Simulation/1"


def _new_progress(start_scene: str) -> dict[str, Any]:
    return {
        "status": "running",
        "phase": "scene_enter",
        "current_scene_id": start_scene,
        "next_scene_id": None,
        "scene_step": 0,
        "processed_events": [],
        "scene_sequence": [],
        "choice_sequence": [],
        "visits": {},
    }


def initial_payload(bundle: Bundle) -> dict[str, Any]:
    fixed: dict[str, int] = {}
    reference: dict[str, float] = {}
    booleans: dict[str, bool] = {}
    for key_id, definition in bundle.state.keys.items():
        if isinstance(definition, FixedKey):
            fixed[key_id] = definition.initial
            reference[key_id] = float(Decimal(definition.initial_text))
        else:
            booleans[key_id] = definition.initial
    return {
        "format": SIM_FORMAT,
        "version_id": bundle.version_id,
        "run_id": bundle.run_id,
        "progress": _new_progress(bundle.story.start_scene),
        "world": {
            "fixed": fixed,
            "reference": reference,
            "bool": booleans,
        },
        "ledger": [],
        "diagnostics": [],
        "first_exceeded": None,
        "reference_mismatches": [],
        "fixed_is_authoritative": True,
    }


def _check_payload(bundle: Bundle, payload: dict[str, Any]) -> None:
    if payload.get("format") != SIM_FORMAT:
        raise DigestError(
            "unsupported simulation snapshot format",
            {"expected": SIM_FORMAT, "actual": payload.get("format")},
        )
    if payload.get("version_id") != bundle.version_id:
        raise DigestError(
            "simulation snapshot version does not match the validated input",
            {
                "expected": bundle.version_id,
                "actual": payload.get("version_id"),
            },
        )
    world = payload.get("world")
    if not isinstance(world, dict):
        raise DigestError("simulation snapshot has no world state")
    fixed = world.get("fixed")
    reference = world.get("reference")
    booleans = world.get("bool")
    if not all(isinstance(item, dict) for item in (fixed, reference, booleans)):
        raise DigestError("simulation snapshot world state is malformed")
    expected_keys = set(bundle.state.keys)
    if set(fixed) | set(booleans) != expected_keys:
        raise DigestError(
            "simulation snapshot state keys do not match the declarations",
            {
                "missing": sorted(expected_keys - (set(fixed) | set(booleans))),
                "extra": sorted((set(fixed) | set(booleans)) - expected_keys),
            },
        )
    if set(reference) != set(fixed):
        raise DigestError("simulation snapshot reference state is incomplete")
    ledger = payload.get("ledger")
    if not isinstance(ledger, list):
        raise DigestError("simulation snapshot ledger is malformed")
    for index, record in enumerate(ledger):
        if not isinstance(record, dict) or record.get("record_id") != index:
            raise DigestError(
                "simulation snapshot ledger is not sequential",
                {"entry_index": index},
            )
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        raise DigestError("simulation snapshot progress is malformed")


class SimulationKernel:
    def __init__(self, bundle: Bundle, payload: dict[str, Any] | None = None) -> None:
        self.bundle = bundle
        self.payload = copy.deepcopy(payload) if payload is not None else initial_payload(bundle)
        _check_payload(bundle, self.payload)
        self.progress: dict[str, Any] = self.payload["progress"]
        self.fixed: dict[str, int] = self.payload["world"]["fixed"]
        self.reference: dict[str, float] = self.payload["world"]["reference"]
        self.booleans: dict[str, bool] = self.payload["world"]["bool"]
        self.ledger: list[dict[str, Any]] = self.payload["ledger"]
        self.diagnostics: list[dict[str, Any]] = self.payload["diagnostics"]

    @property
    def complete(self) -> bool:
        return self.progress.get("status") == "complete"

    @property
    def scene(self) -> SceneDef:
        scene_id = self.progress.get("current_scene_id")
        scene = self.bundle.story.scenes.get(scene_id)
        if scene is None:
            raise DomainError(
                "E_SCENE_REF",
                f"simulation points at missing scene: {scene_id}",
                {"scene_id": scene_id},
            )
        return scene

    def to_payload(self) -> dict[str, Any]:
        self.payload["ledger"] = self.ledger
        self.payload["diagnostics"] = self.diagnostics
        self.payload["world"] = {
            "fixed": self.fixed,
            "reference": self.reference,
            "bool": self.booleans,
        }
        self.payload["progress"] = self.progress
        return copy.deepcopy(self.payload)

    def _record_numeric_diagnostic(
        self,
        *,
        frame_index: int,
        scene_id: str,
        event_id: str | None,
        branch_id: str | None,
        phase: str,
        key: str,
        fixed_value: int,
        reference_value: float,
    ) -> dict[str, Any]:
        abs_error = error_decimal(
            fixed_value, reference_value, self.bundle.state.fixed_scale
        )
        tolerance = self.bundle.state.residual_tolerance
        exceeded = abs_error > tolerance
        diagnostic = {
            "frame_index": frame_index,
            "scene_id": scene_id,
            "event_id": event_id,
            "branch_id": branch_id,
            "phase": phase,
            "state_key": key,
            "fixed_value_micros": fixed_value,
            "fixed_value": fixed_text(fixed_value, self.bundle.state.fixed_scale),
            "reference_value": reference_value,
            "absolute_error": format(abs_error, "f"),
            "tolerance": self.bundle.state.residual_tolerance_text,
            "exceeded_tolerance": exceeded,
        }
        self.diagnostics.append(diagnostic)
        if exceeded and self.payload.get("first_exceeded") is None:
            self.payload["first_exceeded"] = {
                "frame_index": frame_index,
                "scene_id": scene_id,
                "event_id": event_id,
                "branch_id": branch_id,
                "phase": phase,
                "state_key": key,
                "fixed_value_micros": fixed_value,
                "fixed_value": diagnostic["fixed_value"],
                "reference_value": reference_value,
                "absolute_error": diagnostic["absolute_error"],
                "tolerance": self.bundle.state.residual_tolerance_text,
            }
        return diagnostic

    def _evaluate_condition(
        self,
        condition: Condition,
        *,
        frame_index: int,
        scene_id: str,
        event_id: str | None,
        branch_id: str | None,
    ) -> dict[str, Any]:
        definition = self.bundle.state.keys[condition.key]
        if isinstance(definition, FixedKey):
            actual_fixed = self.fixed[condition.key]
            actual_reference = self.reference[condition.key]
            threshold_reference = float(Decimal(condition.threshold_text))
            fixed_passed = compare_fixed(actual_fixed, condition.op, int(condition.value))
            float_passed = compare_float(
                actual_reference, condition.op, threshold_reference
            )
            self._record_numeric_diagnostic(
                frame_index=frame_index,
                scene_id=scene_id,
                event_id=event_id,
                branch_id=branch_id,
                phase="precondition",
                key=condition.key,
                fixed_value=actual_fixed,
                reference_value=actual_reference,
            )
            result = {
                "key": condition.key,
                "kind": "fixed",
                "op": condition.op,
                "threshold_micros": int(condition.value),
                "threshold": condition.threshold_text,
                "actual_micros": actual_fixed,
                "actual": fixed_text(actual_fixed, self.bundle.state.fixed_scale),
                "reference_actual": actual_reference,
                "reference_threshold": threshold_reference,
                "fixed_passed": fixed_passed,
                "reference_passed": float_passed,
            }
        else:
            actual = self.booleans[condition.key]
            expected = bool(condition.value)
            passed = actual == expected if condition.op in {"eq", "is"} else actual != expected
            result = {
                "key": condition.key,
                "kind": "bool",
                "op": condition.op,
                "threshold": expected,
                "actual": actual,
                "reference_actual": actual,
                "fixed_passed": passed,
                "reference_passed": passed,
            }
        if result["fixed_passed"] != result["reference_passed"]:
            self.payload.setdefault("reference_mismatches", []).append(
                {
                    "frame_index": frame_index,
                    "scene_id": scene_id,
                    "event_id": event_id,
                    "branch_id": branch_id,
                    "state_key": condition.key,
                    "fixed_passed": result["fixed_passed"],
                    "reference_passed": result["reference_passed"],
                }
            )
        return result

    def _evaluate_conditions(
        self,
        conditions: tuple[Condition, ...],
        *,
        frame_index: int,
        scene_id: str,
        event_id: str | None,
        branch_id: str | None,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        results = [
            self._evaluate_condition(
                condition,
                frame_index=frame_index,
                scene_id=scene_id,
                event_id=event_id,
                branch_id=branch_id,
            )
            for condition in conditions
        ]
        fixed_ok = all(result["fixed_passed"] for result in results)
        reference_ok = all(result["reference_passed"] for result in results)
        return results, fixed_ok, reference_ok

    def _apply_effects(
        self,
        effects: tuple[Effect, ...],
        *,
        frame_index: int,
        scene_id: str,
        event_id: str | None,
        branch_id: str | None,
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for effect in effects:
            definition = self.bundle.state.keys[effect.key]
            if isinstance(definition, FixedKey):
                before_fixed = self.fixed[effect.key]
                before_reference = self.reference[effect.key]
                declared_reference = float(Decimal(effect.value_text))
                if effect.op == "add":
                    after_fixed = before_fixed + int(effect.value)
                    after_reference = before_reference + declared_reference
                elif effect.op == "set":
                    after_fixed = int(effect.value)
                    after_reference = declared_reference
                else:
                    raise DomainError(
                        "E_OP", f"unsupported fixed effect op: {effect.op}"
                    )
                if after_fixed < definition.minimum or after_fixed > definition.maximum:
                    raise DomainError(
                        "E_DOMAIN",
                        f"effect pushes {effect.key} outside its domain",
                        {
                            "scene_id": scene_id,
                            "event_id": event_id,
                            "branch_id": branch_id,
                            "state_key": effect.key,
                            "after_micros": after_fixed,
                            "minimum_micros": definition.minimum,
                            "maximum_micros": definition.maximum,
                        },
                    )
                self.fixed[effect.key] = after_fixed
                self.reference[effect.key] = after_reference
                self._record_numeric_diagnostic(
                    frame_index=frame_index,
                    scene_id=scene_id,
                    event_id=event_id,
                    branch_id=branch_id,
                    phase="effect",
                    key=effect.key,
                    fixed_value=after_fixed,
                    reference_value=after_reference,
                )
                if after_fixed != before_fixed:
                    changes.append(
                        {
                            "key": effect.key,
                            "kind": "fixed",
                            "op": effect.op,
                            "before_micros": before_fixed,
                            "before": fixed_text(
                                before_fixed, self.bundle.state.fixed_scale
                            ),
                            "after_micros": after_fixed,
                            "after": fixed_text(
                                after_fixed, self.bundle.state.fixed_scale
                            ),
                            "fixed_value": after_fixed,
                            "fixed_value_text": fixed_text(
                                after_fixed, self.bundle.state.fixed_scale
                            ),
                            "reference_value": after_reference,
                        }
                    )
            else:
                before = self.booleans[effect.key]
                if effect.op == "set":
                    after = bool(effect.value)
                elif effect.op == "toggle":
                    after = not before
                else:
                    raise DomainError(
                        "E_OP", f"unsupported boolean effect op: {effect.op}"
                    )
                self.booleans[effect.key] = after
                if after != before:
                    changes.append(
                        {
                            "key": effect.key,
                            "kind": "bool",
                            "op": effect.op,
                            "before": before,
                            "after": after,
                            "fixed_value": int(after),
                            "fixed_value_text": "true" if after else "false",
                            "reference_value": after,
                        }
                    )
        return changes

    def _base_record(
        self,
        *,
        scene: SceneDef,
        kind: str,
        logical_time: int,
        event_id: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        record_id = len(self.ledger)
        return {
            "record_id": record_id,
            "frame_index": record_id,
            "logical_time": logical_time,
            "scene_id": scene.id,
            "scene_title": scene.title,
            "kind": kind,
            "event_id": event_id,
            "branch_id": branch_id,
            "causal_parent_record_id": record_id - 1 if record_id else None,
            "scene_sequence": list(self.progress["scene_sequence"]),
            "upstream_choices": copy.deepcopy(self.progress["choice_sequence"]),
            "preconditions": [],
            "state_changes": [],
            "reference_reachable": True,
        }

    def _append_record(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        satisfied = [
            {
                "key": condition["key"],
                "op": condition["op"],
                "actual": condition.get("actual"),
                "threshold": condition.get("threshold"),
                "fixed_passed": condition["fixed_passed"],
                "reference_passed": condition["reference_passed"],
            }
            for condition in record.get("preconditions", [])
            if condition.get("fixed_passed")
        ]
        record["satisfied_preconditions"] = satisfied
        for change in record.get("state_changes", []):
            change["record_id"] = record["record_id"]
            change["trigger_event_id"] = record.get("event_id")
            change["trigger_branch_id"] = record.get("branch_id")
            change["satisfied_preconditions"] = copy.deepcopy(satisfied)
            change["upstream_choices"] = copy.deepcopy(record["upstream_choices"])
            change["scene_sequence"] = list(record["scene_sequence"])
            change["causal_parent_record_id"] = record["causal_parent_record_id"]
        self.ledger.append(record)
        self.progress["scene_step"] += 1
        return record, False

    def _choose_event(
        self, scene: SceneDef
    ) -> tuple[EventDef, list[dict[str, Any]], bool] | None:
        processed = set(self.progress["processed_events"])
        ordered = sorted(
            scene.events, key=lambda item: (-item.priority, item.declaration_index)
        )
        for event in ordered:
            if event.id in processed:
                continue
            results, fixed_ok, reference_ok = self._evaluate_conditions(
                event.preconditions,
                frame_index=len(self.ledger),
                scene_id=scene.id,
                event_id=event.id,
                branch_id=None,
            )
            if fixed_ok:
                return event, results, reference_ok
        return None

    def _choose_branch(
        self, scene: SceneDef
    ) -> tuple[Any, list[dict[str, Any]], bool] | None:
        if not scene.branches:
            return None
        valid: list[tuple[Any, list[dict[str, Any]], bool]] = []
        for branch in scene.branches:
            results, fixed_ok, reference_ok = self._evaluate_conditions(
                branch.preconditions,
                frame_index=len(self.ledger),
                scene_id=scene.id,
                event_id=None,
                branch_id=branch.id,
            )
            if fixed_ok:
                valid.append((branch, results, reference_ok))
        requested = self.bundle.run.choices.get(scene.id)
        if requested is not None:
            for candidate in valid:
                if candidate[0].id == requested:
                    return candidate
            raise InputError(
                "E_CHOICE",
                f"requested branch {requested} is not available in scene {scene.id}",
                {"scene_id": scene.id, "branch_id": requested},
            )
        defaults = [candidate for candidate in valid if candidate[0].is_default]
        if defaults:
            return sorted(defaults, key=lambda item: item[0].declaration_index)[0]
        if len(valid) == 1:
            return valid[0]
        if not valid:
            raise InputError(
                "E_BRANCH",
                f"no branch is available in scene {scene.id}",
                {"scene_id": scene.id},
            )
        raise InputError(
            "E_CHOICE",
            f"scene {scene.id} has multiple available branches and no default",
            {
                "scene_id": scene.id,
                "available_branches": [candidate[0].id for candidate in valid],
            },
        )

    def step(self) -> tuple[dict[str, Any] | None, bool]:
        if self.complete:
            return None, True
        if len(self.ledger) >= self.bundle.run.max_steps:
            raise InputError(
                "E_LIMIT",
                "simulation exceeded max_steps",
                {"max_steps": self.bundle.run.max_steps},
            )
        while True:
            phase = self.progress["phase"]
            scene = self.scene
            if phase == "scene_enter":
                if self.progress["current_scene_id"] not in self.progress["visits"]:
                    self.progress["visits"][scene.id] = 0
                self.progress["visits"][scene.id] += 1
                self.progress["scene_sequence"].append(scene.id)
                self.progress["processed_events"] = []
                self.progress["scene_step"] = 0
                record = self._base_record(
                    scene=scene, kind="scene_enter", logical_time=scene.start_time
                )
                self.progress["phase"] = "events"
                return self._append_record(record)

            if phase == "events":
                frame_index = len(self.ledger)
                logical_time = scene.start_time + self.progress["scene_step"]
                chosen = self._choose_event(scene)
                if chosen is None:
                    self.progress["phase"] = "branch"
                    continue
                event, conditions, reference_ok = chosen
                record = self._base_record(
                    scene=scene,
                    kind="event",
                    logical_time=logical_time,
                    event_id=event.id,
                )
                record["priority"] = event.priority
                record["preconditions"] = conditions
                record["reference_reachable"] = reference_ok
                record["state_changes"] = self._apply_effects(
                    event.effects,
                    frame_index=frame_index,
                    scene_id=scene.id,
                    event_id=event.id,
                    branch_id=None,
                )
                self.progress["processed_events"].append(event.id)
                return self._append_record(record)

            if phase == "branch":
                chosen = self._choose_branch(scene)
                if chosen is None:
                    self.progress["phase"] = "advance"
                    self.progress["next_scene_id"] = None
                    continue
                branch, conditions, reference_ok = chosen
                frame_index = len(self.ledger)
                logical_time = scene.start_time + self.progress["scene_step"]
                record = self._base_record(
                    scene=scene,
                    kind="branch",
                    logical_time=logical_time,
                    branch_id=branch.id,
                )
                record["preconditions"] = conditions
                record["reference_reachable"] = reference_ok
                record["next_scene_id"] = branch.next_scene
                record["state_changes"] = self._apply_effects(
                    branch.effects,
                    frame_index=frame_index,
                    scene_id=scene.id,
                    event_id=None,
                    branch_id=branch.id,
                )
                self.progress["choice_sequence"].append(
                    {"scene_id": scene.id, "branch_id": branch.id}
                )
                self.progress["next_scene_id"] = branch.next_scene
                self.progress["phase"] = "advance"
                return self._append_record(record)

            if phase == "advance":
                next_scene = self.progress.get("next_scene_id")
                if next_scene is None:
                    self.progress["status"] = "complete"
                    self.progress["phase"] = "complete"
                    self.progress["current_scene_id"] = scene.id
                    return None, True
                if next_scene not in self.bundle.story.scenes:
                    raise InputError(
                        "E_BRANCH_REF",
                        f"simulation reached missing scene: {next_scene}",
                        {"scene_id": next_scene},
                    )
                self.progress["current_scene_id"] = next_scene
                self.progress["next_scene_id"] = None
                self.progress["phase"] = "scene_enter"
                continue

            if phase == "complete":
                self.progress["status"] = "complete"
                return None, True
            raise DomainError(
                "E_STATE",
                f"unknown simulation phase: {phase}",
                {"phase": phase},
            )

    def summary(self) -> dict[str, Any]:
        state_view: dict[str, Any] = {}
        for key_id, definition in self.bundle.state.keys.items():
            if isinstance(definition, FixedKey):
                state_view[key_id] = {
                    "kind": "fixed",
                    "micros": self.fixed[key_id],
                    "value": fixed_text(
                        self.fixed[key_id], self.bundle.state.fixed_scale
                    ),
                    "reference_value": self.reference[key_id],
                }
            elif isinstance(definition, BoolKey):
                state_view[key_id] = {
                    "kind": "bool",
                    "value": self.booleans[key_id],
                }
        records = [
            {
                "frame_index": record["frame_index"],
                "logical_time": record["logical_time"],
                "scene_id": record["scene_id"],
                "kind": record["kind"],
                "event_id": record.get("event_id"),
                "branch_id": record.get("branch_id"),
                "record_id": record["record_id"],
            }
            for record in self.ledger
        ]
        return {
            "status": "complete" if self.complete else "running",
            "fixed_is_authoritative": True,
            "reference_path_role": "diagnostic_only",
            "version_id": self.bundle.version_id,
            "run_id": self.bundle.run_id,
            "fixed_scale": self.bundle.state.fixed_scale,
            "residual_tolerance": self.bundle.state.residual_tolerance_text,
            "world": state_view,
            "records": records,
            "event_sequence": [
                record["event_id"]
                for record in self.ledger
                if record["kind"] == "event"
            ],
            "choice_sequence": copy.deepcopy(self.progress["choice_sequence"]),
            "scene_sequence": copy.deepcopy(self.progress["scene_sequence"]),
            "first_exceeded": copy.deepcopy(self.payload.get("first_exceeded")),
            "reference_mismatch_count": len(
                self.payload.get("reference_mismatches", [])
            ),
        }

