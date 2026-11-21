from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .models import (
    ClockSpec,
    EVENT_CODES,
    INJECTION_TYPES,
    Injection,
    Plan,
    PlanStep,
    ROUTE_MODES,
    Route,
    SCHEMA_VERSION,
    Snapshot,
    StepWindow,
)
from .util import (
    digest_value,
    read_json_file,
    require_bool,
    require_dict,
    require_int,
    require_list,
    require_str,
    sha256_text,
)


E_BAD_PLAN = "E_BAD_PLAN"
E_CYCLE = "E_CYCLE"
E_BAD_SERVICE = "E_BAD_SERVICE"
E_BAD_CLOCK = "E_BAD_CLOCK"
E_BAD_INJECT = "E_BAD_INJECT"


@dataclass(frozen=True)
class LoadedDocuments:
    plans_doc: dict[str, Any]
    services_doc: dict[str, Any]
    clock_doc: dict[str, Any]
    injections_doc: dict[str, Any]
    source_digests: dict[str, str]


def _check_schema(doc: dict[str, Any], *, code: str, label: str) -> None:
    schema_version = require_int(
        doc.get("schema_version"),
        code=code,
        path=f"{label}.schema_version",
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise BusinessError(
            code,
            f"{label}.schema_version must be {SCHEMA_VERSION}",
            {"path": f"{label}.schema_version", "value": schema_version},
        )


def _parse_steps(raw_steps: Any, *, plan_name: str) -> tuple[PlanStep, ...]:
    steps_list = require_list(
        raw_steps,
        code=E_BAD_PLAN,
        path=f"plan[{plan_name}].steps",
    )
    if not steps_list:
        raise BusinessError(
            E_BAD_PLAN,
            f"plan[{plan_name}] must contain at least one step",
            {"plan": plan_name},
        )

    steps: list[PlanStep] = []
    seen_seq: dict[int, int] = {}
    for index, raw_step in enumerate(steps_list):
        path = f"plan[{plan_name}].steps[{index}]"
        item = require_dict(raw_step, code=E_BAD_PLAN, path=path)
        seq = require_int(item.get("seq"), code=E_BAD_PLAN, path=f"{path}.seq", minimum=1)
        if seq in seen_seq:
            raise BusinessError(
                E_BAD_PLAN,
                f"duplicate step sequence {seq} in plan {plan_name}",
                {
                    "plan": plan_name,
                    "step": seq,
                    "first_index": seen_seq[seq],
                    "second_index": index,
                },
            )
        seen_seq[seq] = index
        name = require_str(item.get("name"), code=E_BAD_PLAN, path=f"{path}.name")
        depends_raw = require_list(
            item.get("depends_on"),
            code=E_BAD_PLAN,
            path=f"{path}.depends_on",
        )
        depends: list[int] = []
        for dep_index, raw_dep in enumerate(depends_raw):
            dep = require_int(
                raw_dep,
                code=E_BAD_PLAN,
                path=f"{path}.depends_on[{dep_index}]",
                minimum=1,
            )
            if dep in depends:
                raise BusinessError(
                    E_BAD_PLAN,
                    f"duplicate dependency {dep} in plan {plan_name} step {seq}",
                    {"plan": plan_name, "step": seq, "dependency": dep},
                )
            depends.append(dep)
        expected_event = require_str(
            item.get("expected_event"),
            code=E_BAD_PLAN,
            path=f"{path}.expected_event",
        )
        if expected_event not in EVENT_CODES:
            raise BusinessError(
                E_BAD_PLAN,
                f"invalid expected_event {expected_event!r}",
                {
                    "plan": plan_name,
                    "step": seq,
                    "expected_event": expected_event,
                    "allowed": sorted(EVENT_CODES),
                },
            )
        timeout = require_int(
            item.get("timeout_logical_ms"),
            code=E_BAD_PLAN,
            path=f"{path}.timeout_logical_ms",
            minimum=1,
        )
        skippable = require_bool(
            item.get("skippable"),
            code=E_BAD_PLAN,
            path=f"{path}.skippable",
        )
        steps.append(
            PlanStep(
                seq=seq,
                name=name,
                depends_on=tuple(sorted(depends)),
                expected_event=expected_event,
                timeout_logical_ms=timeout,
                skippable=skippable,
            )
        )

    seq_set = set(seen_seq)
    for step in steps:
        for dep in step.depends_on:
            if dep not in seq_set:
                raise BusinessError(
                    E_BAD_PLAN,
                    f"step {step.seq} references missing prerequisite {dep}",
                    {"plan": plan_name, "step": step.seq, "dependency": dep},
                )
        if step.seq in step.depends_on:
            raise BusinessError(
                E_BAD_PLAN,
                f"step {step.seq} depends on itself",
                {"plan": plan_name, "step": step.seq},
            )
    return tuple(sorted(steps, key=lambda step: step.seq))


def _detect_cycle(plan: Plan) -> None:
    indegree = {step.seq: len(step.depends_on) for step in plan.steps}
    dependents: dict[int, list[int]] = {step.seq: [] for step in plan.steps}
    for step in plan.steps:
        for dep in step.depends_on:
            dependents[dep].append(step.seq)
    ready = sorted(seq for seq, degree in indegree.items() if degree == 0)
    visited: list[int] = []
    while ready:
        seq = ready.pop(0)
        visited.append(seq)
        for child in sorted(dependents[seq]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort()
    if len(visited) != len(plan.steps):
        cycle_nodes = sorted(seq for seq, degree in indegree.items() if degree > 0)
        raise BusinessError(
            E_CYCLE,
            f"dependency cycle detected in plan {plan.name}",
            {"plan": plan.name, "cycle_nodes": cycle_nodes},
        )


def _parse_plans(doc: dict[str, Any]) -> dict[str, Plan]:
    _check_schema(doc, code=E_BAD_PLAN, label="plans")
    raw_plans = require_list(doc.get("plans"), code=E_BAD_PLAN, path="plans.plans")
    if not raw_plans:
        raise BusinessError(E_BAD_PLAN, "plans.plans must not be empty")
    plans: dict[str, Plan] = {}
    for index, raw_plan in enumerate(raw_plans):
        path = f"plans.plans[{index}]"
        item = require_dict(raw_plan, code=E_BAD_PLAN, path=path)
        name = require_str(item.get("name"), code=E_BAD_PLAN, path=f"{path}.name")
        if name in plans:
            raise BusinessError(
                E_BAD_PLAN,
                f"duplicate plan name: {name}",
                {"plan": name},
            )
        plan = Plan(name=name, steps=_parse_steps(item.get("steps"), plan_name=name))
        _detect_cycle(plan)
        plans[name] = plan
    return plans


def _plan_sections(
    doc: dict[str, Any],
    *,
    code: str,
    label: str,
    key: str,
) -> dict[str, dict[str, Any]]:
    _check_schema(doc, code=code, label=label)
    raw_plans = require_list(doc.get("plans"), code=code, path=f"{label}.plans")
    sections: dict[str, dict[str, Any]] = {}
    for index, raw_section in enumerate(raw_plans):
        path = f"{label}.plans[{index}]"
        section = require_dict(raw_section, code=code, path=path)
        name = require_str(section.get("name"), code=code, path=f"{path}.name")
        if name in sections:
            raise BusinessError(
                code,
                f"duplicate plan section name: {name}",
                {"plan": name, "document": label},
            )
        sections[name] = section
    return sections


def _parse_routes(doc: dict[str, Any], plan: Plan) -> tuple[Route, ...]:
    sections = _plan_sections(
        doc,
        code=E_BAD_SERVICE,
        label="services",
        key="routes",
    )
    if plan.name not in sections:
        raise BusinessError(
            E_BAD_SERVICE,
            f"services document has no section for plan {plan.name}",
            {"plan": plan.name},
        )
    section = sections[plan.name]
    raw_routes = require_list(
        section.get("routes"),
        code=E_BAD_SERVICE,
        path=f"services.plan[{plan.name}].routes",
    )
    routes: list[Route] = []
    steps = plan.step_map
    seen_steps: set[int] = set()
    seen_paths: set[str] = set()
    for index, raw_route in enumerate(raw_routes):
        path = f"services.plan[{plan.name}].routes[{index}]"
        item = require_dict(raw_route, code=E_BAD_SERVICE, path=path)
        step = require_int(item.get("step"), code=E_BAD_SERVICE, path=f"{path}.step", minimum=1)
        if step not in steps:
            raise BusinessError(
                E_BAD_SERVICE,
                f"route references unknown step {step}",
                {"plan": plan.name, "step": step},
            )
        if step in seen_steps:
            raise BusinessError(
                E_BAD_SERVICE,
                f"duplicate route for step {step}",
                {"plan": plan.name, "step": step},
            )
        seen_steps.add(step)
        route_path = require_str(item.get("path"), code=E_BAD_SERVICE, path=f"{path}.path")
        if not route_path.startswith("/"):
            raise BusinessError(
                E_BAD_SERVICE,
                f"route path must start with '/': {route_path}",
                {"plan": plan.name, "step": step, "path": route_path},
            )
        if route_path in seen_paths:
            raise BusinessError(
                E_BAD_SERVICE,
                f"duplicate route path: {route_path}",
                {"plan": plan.name, "path": route_path},
            )
        seen_paths.add(route_path)
        status = require_int(
            item.get("status"),
            code=E_BAD_SERVICE,
            path=f"{path}.status",
            minimum=100,
        )
        if status > 599:
            raise BusinessError(
                E_BAD_SERVICE,
                "route status must be between 100 and 599",
                {"plan": plan.name, "step": step, "status": status},
            )
        delay = require_int(
            item.get("delay_logical_ms"),
            code=E_BAD_SERVICE,
            path=f"{path}.delay_logical_ms",
            minimum=0,
        )
        body_value = item.get("body")
        if not isinstance(body_value, str):
            raise BusinessError(
                E_BAD_SERVICE,
                f"{path}.body: expected string",
                {"path": f"{path}.body"},
            )
        content_type = require_str(
            item.get("content_type"),
            code=E_BAD_SERVICE,
            path=f"{path}.content_type",
        )
        mode = require_str(item.get("mode"), code=E_BAD_SERVICE, path=f"{path}.mode")
        if mode not in ROUTE_MODES:
            raise BusinessError(
                E_BAD_SERVICE,
                f"invalid route mode {mode!r}",
                {
                    "plan": plan.name,
                    "step": step,
                    "mode": mode,
                    "allowed": sorted(ROUTE_MODES),
                },
            )
        body_hash = sha256_text(body_value)
        given_hash = item.get("body_sha256")
        if given_hash is not None:
            if not isinstance(given_hash, str) or given_hash != body_hash:
                raise BusinessError(
                    E_BAD_SERVICE,
                    "route body_sha256 does not match body content",
                    {
                        "plan": plan.name,
                        "step": step,
                        "expected": body_hash,
                        "actual": given_hash,
                    },
                )
        routes.append(
            Route(
                step=step,
                path=route_path,
                status=status,
                delay_logical_ms=delay,
                body=body_value,
                content_type=content_type,
                mode=mode,
                body_sha256=body_hash,
            )
        )
    missing = sorted(set(steps) - seen_steps)
    if missing:
        raise BusinessError(
            E_BAD_SERVICE,
            f"missing routes for steps: {missing}",
            {"plan": plan.name, "missing_steps": missing},
        )
    return tuple(sorted(routes, key=lambda route: route.step))


def _parse_clock(doc: dict[str, Any], plan: Plan) -> ClockSpec:
    sections = _plan_sections(
        doc,
        code=E_BAD_CLOCK,
        label="clock",
        key="step_windows",
    )
    if plan.name not in sections:
        raise BusinessError(
            E_BAD_CLOCK,
            f"clock document has no section for plan {plan.name}",
            {"plan": plan.name},
        )
    section = sections[plan.name]
    start_ms = require_int(
        section.get("start_ms"),
        code=E_BAD_CLOCK,
        path=f"clock.plan[{plan.name}].start_ms",
        minimum=0,
    )
    tick = require_dict(
        section.get("tick"),
        code=E_BAD_CLOCK,
        path=f"clock.plan[{plan.name}].tick",
    )
    tick_logical = require_int(
        tick.get("logical_ms"),
        code=E_BAD_CLOCK,
        path=f"clock.plan[{plan.name}].tick.logical_ms",
        minimum=1,
    )
    tick_real = require_int(
        tick.get("real_ms"),
        code=E_BAD_CLOCK,
        path=f"clock.plan[{plan.name}].tick.real_ms",
        minimum=1,
    )
    raw_windows = require_list(
        section.get("step_windows"),
        code=E_BAD_CLOCK,
        path=f"clock.plan[{plan.name}].step_windows",
    )
    steps = plan.step_map
    windows: list[StepWindow] = []
    seen: set[int] = set()
    for index, raw_window in enumerate(raw_windows):
        path = f"clock.plan[{plan.name}].step_windows[{index}]"
        item = require_dict(raw_window, code=E_BAD_CLOCK, path=path)
        step = require_int(item.get("step"), code=E_BAD_CLOCK, path=f"{path}.step", minimum=1)
        if step not in steps:
            raise BusinessError(
                E_BAD_CLOCK,
                f"clock window references unknown step {step}",
                {"plan": plan.name, "step": step},
            )
        if step in seen:
            raise BusinessError(
                E_BAD_CLOCK,
                f"duplicate clock window for step {step}",
                {"plan": plan.name, "step": step},
            )
        seen.add(step)
        window_start = require_int(
            item.get("start_ms"),
            code=E_BAD_CLOCK,
            path=f"{path}.start_ms",
            minimum=0,
        )
        window_end = require_int(
            item.get("end_ms"),
            code=E_BAD_CLOCK,
            path=f"{path}.end_ms",
            minimum=1,
        )
        if window_start < start_ms:
            raise BusinessError(
                E_BAD_CLOCK,
                "step window starts before the logical clock epoch",
                {
                    "plan": plan.name,
                    "step": step,
                    "start_ms": window_start,
                    "clock_start_ms": start_ms,
                },
            )
        if window_end != window_start + steps[step].timeout_logical_ms:
            raise BusinessError(
                E_BAD_CLOCK,
                "step window width must equal the step timeout budget",
                {
                    "plan": plan.name,
                    "step": step,
                    "window_start_ms": window_start,
                    "window_end_ms": window_end,
                    "timeout_logical_ms": steps[step].timeout_logical_ms,
                },
            )
        windows.append(StepWindow(step=step, start_ms=window_start, end_ms=window_end))
    missing = sorted(set(steps) - seen)
    if missing:
        raise BusinessError(
            E_BAD_CLOCK,
            f"missing clock windows for steps: {missing}",
            {"plan": plan.name, "missing_steps": missing},
        )
    return ClockSpec(
        start_ms=start_ms,
        tick_logical_ms=tick_logical,
        tick_real_ms=tick_real,
        windows=tuple(sorted(windows, key=lambda window: window.step)),
    )


def _parse_injections(
    doc: dict[str, Any],
    plan: Plan,
    clock: ClockSpec,
) -> tuple[Injection, ...]:
    sections = _plan_sections(
        doc,
        code=E_BAD_INJECT,
        label="injections",
        key="injections",
    )
    if plan.name not in sections:
        raise BusinessError(
            E_BAD_INJECT,
            f"injections document has no section for plan {plan.name}",
            {"plan": plan.name},
        )
    section = sections[plan.name]
    raw_injections = require_list(
        section.get("injections"),
        code=E_BAD_INJECT,
        path=f"injections.plan[{plan.name}].injections",
    )
    steps = plan.step_map
    windows = clock.window_map
    seen_ids: set[str] = set()
    injections: list[Injection] = []
    for index, raw_injection in enumerate(raw_injections):
        path = f"injections.plan[{plan.name}].injections[{index}]"
        item = require_dict(raw_injection, code=E_BAD_INJECT, path=path)
        injection_id = require_str(
            item.get("id"),
            code=E_BAD_INJECT,
            path=f"{path}.id",
        )
        if injection_id in seen_ids:
            raise BusinessError(
                E_BAD_INJECT,
                f"duplicate injection id: {injection_id}",
                {"injection": injection_id},
            )
        seen_ids.add(injection_id)
        step = require_int(
            item.get("step"),
            code=E_BAD_INJECT,
            path=f"{path}.step",
            minimum=1,
        )
        if step not in steps:
            raise BusinessError(
                E_BAD_INJECT,
                f"injection references unknown step {step}",
                {"injection": injection_id, "step": step},
            )
        at_ms = require_int(
            item.get("at_ms"),
            code=E_BAD_INJECT,
            path=f"{path}.at_ms",
            minimum=0,
        )
        window = windows[step]
        if not (window.start_ms <= at_ms < window.end_ms):
            raise BusinessError(
                E_BAD_INJECT,
                "injection logical time is outside its step window",
                {
                    "injection": injection_id,
                    "step": step,
                    "at_ms": at_ms,
                    "window_start_ms": window.start_ms,
                    "window_end_ms": window.end_ms,
                },
            )
        injection_type = require_str(
            item.get("type"),
            code=E_BAD_INJECT,
            path=f"{path}.type",
        )
        if injection_type not in INJECTION_TYPES:
            raise BusinessError(
                E_BAD_INJECT,
                f"invalid injection type {injection_type!r}",
                {
                    "injection": injection_id,
                    "type": injection_type,
                    "allowed": sorted(INJECTION_TYPES),
                },
            )
        rollback_ms = item.get("rollback_ms", 0)
        rollback_ms = require_int(
            rollback_ms,
            code=E_BAD_INJECT,
            path=f"{path}.rollback_ms",
            minimum=0,
        )
        if injection_type == "clock_rollback" and rollback_ms <= 0:
            raise BusinessError(
                E_BAD_INJECT,
                "clock_rollback injection requires rollback_ms > 0",
                {"injection": injection_id},
            )
        if injection_type != "clock_rollback" and rollback_ms != 0:
            raise BusinessError(
                E_BAD_INJECT,
                "rollback_ms is only valid for clock_rollback injections",
                {"injection": injection_id, "type": injection_type},
            )
        description = item.get("description", "")
        if not isinstance(description, str):
            raise BusinessError(
                E_BAD_INJECT,
                f"{path}.description: expected string",
                {"path": f"{path}.description"},
            )
        injections.append(
            Injection(
                injection_id=injection_id,
                step=step,
                at_ms=at_ms,
                injection_type=injection_type,
                rollback_ms=rollback_ms,
                description=description,
            )
        )
    return tuple(
        sorted(
            injections,
            key=lambda injection: (
                injection.at_ms,
                injection.step,
                injection.injection_id,
            ),
        )
    )


def load_snapshot(
    plans_path: str | Path,
    services_path: str | Path,
    clock_path: str | Path,
    injections_path: str | Path,
    *,
    plan_name: str | None = None,
) -> Snapshot:
    plans_file = Path(plans_path)
    services_file = Path(services_path)
    clock_file = Path(clock_path)
    injections_file = Path(injections_path)
    plans_doc, _, plans_hash = read_json_file(plans_file, E_BAD_PLAN)
    services_doc, _, services_hash = read_json_file(services_file, E_BAD_SERVICE)
    clock_doc, _, clock_hash = read_json_file(clock_file, E_BAD_CLOCK)
    injections_doc, _, injections_hash = read_json_file(
        injections_file, E_BAD_INJECT
    )

    plans = _parse_plans(plans_doc)
    if plan_name is None:
        if len(plans) != 1:
            raise BusinessError(
                E_BAD_PLAN,
                "multiple plans present; select one with --plan",
                {"plans": sorted(plans)},
            )
        plan_name = next(iter(plans))
    if plan_name not in plans:
        raise BusinessError(
            E_BAD_PLAN,
            f"plan not found: {plan_name}",
            {"plan": plan_name, "available": sorted(plans)},
        )
    plan = plans[plan_name]
    routes = _parse_routes(services_doc, plan)
    clock = _parse_clock(clock_doc, plan)
    injections = _parse_injections(injections_doc, plan, clock)

    plan_dict = plan.to_dict()
    routes_dict = [route.to_dict() for route in routes]
    clock_dict = clock.to_dict()
    injection_dicts = [injection.to_dict() for injection in injections]
    content_payload = {
        "plan": plan_dict,
        "routes": routes_dict,
        "clock": clock_dict,
        "injections": injection_dicts,
    }
    source_digests = {
        "plans.json": plans_hash,
        "services.json": services_hash,
        "clock.json": clock_hash,
        "injections.json": injections_hash,
    }
    return Snapshot(
        plan=plan,
        routes=routes,
        clock=clock,
        injections=injections,
        source_digests=source_digests,
        content_digest=digest_value(content_payload),
        plan_digest=digest_value(plan_dict),
        steps_digest=digest_value(plan_dict["steps"]),
        services_digest=digest_value(routes_dict),
        clock_digest=digest_value(clock_dict),
        injections_digest=digest_value(injection_dicts),
    )

