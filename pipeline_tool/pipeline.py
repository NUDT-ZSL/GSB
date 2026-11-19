"""Pipeline description loading, validation and dependency graph construction."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import E_BAD_BUDGET, E_BAD_JSON, E_BAD_PIPELINE, E_CYCLE, E_INPUT_MISSING, PipelineError


def load_json_file(path: str | Path, what: str = "json") -> dict:
    p = Path(path)
    if not p.is_file():
        raise PipelineError(E_INPUT_MISSING, f"{what} file not found", path=str(p))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(
            E_BAD_JSON, f"{what} file is malformed", path=str(p), line=exc.lineno, column=exc.colno
        ) from exc


def _require_int(value, name: str, node: str | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        where = f"node '{node}'" if node else "config"
        raise PipelineError(E_BAD_PIPELINE, f"{where}: '{name}' must be an integer", node=node, field=name)
    return value


@dataclass(frozen=True)
class StageBudget:
    stage: int
    max_concurrency: int
    memory_budget_mib: int
    window_ms: int


@dataclass(frozen=True)
class Node:
    name: str
    stage: int
    parents: tuple[str, ...]
    cost_ms: int
    memory_mib: int
    parallelism: int
    op: dict


@dataclass
class Pipeline:
    name: str
    clock_step_ms: int
    clock_tolerance_ms: int
    stages: dict[int, StageBudget]
    nodes: dict[str, Node]
    stage_nodes: dict[int, list[str]] = field(default_factory=dict)

    def children_of(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {n: [] for n in self.nodes}
        for node in self.nodes.values():
            for parent in node.parents:
                children[parent].append(node.name)
        return children


def validate_pipeline(raw: dict) -> Pipeline:
    if not isinstance(raw, dict):
        raise PipelineError(E_BAD_PIPELINE, "pipeline description must be a JSON object")

    clock = raw.get("clock", {})
    step_ms = _require_int(clock.get("step_ms", 0), "clock.step_ms")
    tolerance_ms = _require_int(clock.get("tolerance_ms", 0), "clock.tolerance_ms")
    if step_ms <= 0 or tolerance_ms < 0:
        raise PipelineError(E_BAD_BUDGET, "clock.step_ms must be > 0 and clock.tolerance_ms >= 0")

    stages: dict[int, StageBudget] = {}
    for entry in raw.get("stages", []):
        idx = _require_int(entry.get("stage"), "stage")
        max_conc = _require_int(entry.get("max_concurrency"), "max_concurrency")
        mem = _require_int(entry.get("memory_budget_mib"), "memory_budget_mib")
        window = _require_int(entry.get("window_ms"), "window_ms")
        if max_conc < 1 or mem <= 0 or window <= 0:
            raise PipelineError(
                E_BAD_BUDGET,
                "stage budgets must satisfy max_concurrency >= 1, memory_budget_mib > 0, window_ms > 0",
                stage=idx,
            )
        if idx in stages:
            raise PipelineError(E_BAD_PIPELINE, "duplicate stage entry", stage=idx)
        stages[idx] = StageBudget(idx, max_conc, mem, window)
    if not stages:
        raise PipelineError(E_BAD_PIPELINE, "pipeline must declare at least one stage")

    nodes: dict[str, Node] = {}
    for entry in raw.get("nodes", []):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise PipelineError(E_BAD_PIPELINE, "every node needs a non-empty string name")
        if name in nodes:
            raise PipelineError(E_BAD_PIPELINE, "duplicate node name", node=name)
        stage = _require_int(entry.get("stage"), "stage", name)
        if stage not in stages:
            raise PipelineError(E_BAD_PIPELINE, "node references undeclared stage", node=name, stage=stage)
        cost = _require_int(entry.get("cost_ms"), "cost_ms", name)
        mem = _require_int(entry.get("memory_mib"), "memory_mib", name)
        par = _require_int(entry.get("parallelism"), "parallelism", name)
        if cost <= 0 or mem <= 0:
            raise PipelineError(E_BAD_PIPELINE, "cost_ms and memory_mib must be positive integers", node=name)
        if par < 0:
            raise PipelineError(E_BAD_PIPELINE, "parallelism must be a non-negative integer", node=name)
        if par > stages[stage].max_concurrency:
            raise PipelineError(
                E_BAD_PIPELINE,
                "parallelism exceeds stage max_concurrency",
                node=name,
                parallelism=par,
                max_concurrency=stages[stage].max_concurrency,
            )
        parents = entry.get("parents", [])
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            raise PipelineError(E_BAD_PIPELINE, "parents must be a list of node names", node=name)
        op = entry.get("op")
        if not isinstance(op, dict) or "type" not in op:
            raise PipelineError(E_BAD_PIPELINE, "node must declare an op object with a type", node=name)
        nodes[name] = Node(name, stage, tuple(parents), cost, mem, par, op)
    if not nodes:
        raise PipelineError(E_BAD_PIPELINE, "pipeline must declare at least one node")

    for node in nodes.values():
        for parent in node.parents:
            if parent not in nodes:
                raise PipelineError(E_BAD_PIPELINE, "parent node does not exist", node=node.name, parent=parent)
            if nodes[parent].stage >= node.stage:
                raise PipelineError(
                    E_BAD_PIPELINE,
                    "parent stage must be strictly smaller than child stage",
                    node=node.name,
                    parent=parent,
                    node_stage=node.stage,
                    parent_stage=nodes[parent].stage,
                )

    _check_acyclic(nodes)

    stage_nodes: dict[int, list[str]] = {s: [] for s in stages}
    for node in nodes.values():
        stage_nodes[node.stage].append(node.name)
    for names in stage_nodes.values():
        names.sort()

    return Pipeline(
        name=str(raw.get("name", "pipeline")),
        clock_step_ms=step_ms,
        clock_tolerance_ms=tolerance_ms,
        stages=stages,
        nodes=nodes,
        stage_nodes=stage_nodes,
    )


def _check_acyclic(nodes: dict[str, Node]) -> None:
    indegree = {name: 0 for name in nodes}
    children: dict[str, list[str]] = {name: [] for name in nodes}
    for node in nodes.values():
        for parent in node.parents:
            indegree[node.name] += 1
            children[parent].append(node.name)
    queue = sorted(n for n, d in indegree.items() if d == 0)
    seen = 0
    while queue:
        current = queue.pop(0)
        seen += 1
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if seen != len(nodes):
        remaining = sorted(n for n, d in indegree.items() if d > 0)
        raise PipelineError(E_CYCLE, "dependency graph contains a cycle", nodes=remaining)


def load_pipeline(path: str | Path) -> Pipeline:
    return validate_pipeline(load_json_file(path, "pipeline"))
