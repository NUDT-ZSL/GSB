"""Load and strongly validate pipeline DAG definition files.

Validation is all-or-nothing: any structural or semantic issue aborts the
load with ``E_DAG`` and a stable, sorted list of field-level issues.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from .errors import DagValidationError, Issue
from .models import Dependency, PipelineDag, Stage

VALID_DEP_KINDS = ("strong", "weak")


def load_dag(path: str) -> PipelineDag:
    if not os.path.isfile(path):
        raise DagValidationError(
            "DAG definition file not found",
            [Issue("E_DAG", "$", "file", "not_found", {"path": path}).to_dict()],
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise DagValidationError(
            "DAG definition is not valid JSON",
            [
                Issue(
                    "E_DAG",
                    "$",
                    "json",
                    "invalid_json",
                    {"line": exc.lineno, "column": exc.colno},
                ).to_dict()
            ],
        ) from exc
    return parse_dag(raw, source_path=os.path.abspath(path))


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_dag(raw: Any, source_path: str | None = None) -> PipelineDag:
    issues: List[Issue] = []
    if not isinstance(raw, dict):
        raise DagValidationError(
            "DAG root must be a JSON object",
            [Issue("E_DAG", "$", "root", "not_object").to_dict()],
        )

    dag_id = raw.get("dag_id")
    if not isinstance(dag_id, str) or not dag_id:
        issues.append(Issue("E_DAG", "$", "dag_id", "missing_or_empty"))
        dag_id = dag_id if isinstance(dag_id, str) else ""

    dag_version = raw.get("dag_version")
    if not isinstance(dag_version, str) or not dag_version:
        issues.append(Issue("E_DAG", "$", "dag_version", "missing_or_empty"))
        dag_version = dag_version if isinstance(dag_version, str) else ""

    budget = raw.get("global_resource_budget")
    if not _is_positive_int(budget):
        issues.append(
            Issue("E_DAG", "$", "global_resource_budget", "must_be_positive_int")
        )
        budget = budget if isinstance(budget, int) else 1

    default_conc = raw.get("default_max_concurrency", 1)
    if not _is_positive_int(default_conc):
        issues.append(
            Issue("E_DAG", "$", "default_max_concurrency", "must_be_positive_int")
        )
        default_conc = 1

    default_timeout = raw.get("default_timeout_ms")
    if default_timeout is not None and not _is_positive_int(default_timeout):
        issues.append(
            Issue("E_DAG", "$", "default_timeout_ms", "must_be_positive_int_or_null")
        )
        default_timeout = None

    raw_stages = raw.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        issues.append(Issue("E_DAG", "$", "stages", "must_be_non_empty_array"))
        raw_stages = raw_stages if isinstance(raw_stages, list) else []

    stages: Dict[str, Stage] = {}
    seen_ids: Dict[str, int] = {}
    parsed_deps: Dict[str, List[Dependency]] = {}

    for index, item in enumerate(raw_stages):
        base = f"stages[{index}]"
        if not isinstance(item, dict):
            issues.append(Issue("E_DAG", base, "stage", "must_be_object"))
            continue
        stage_id = item.get("stage_id")
        label = stage_id if isinstance(stage_id, str) and stage_id else base
        if not isinstance(stage_id, str) or not stage_id:
            issues.append(Issue("E_DAG", base, "stage_id", "missing_or_empty"))
            stage_id = f"__invalid_{index}"
        elif stage_id in seen_ids:
            issues.append(
                Issue(
                    "E_DAG",
                    label,
                    "stage_id",
                    "duplicate_identifier",
                    {"first_index": seen_ids[stage_id], "duplicate_index": index},
                )
            )
            continue
        else:
            seen_ids[stage_id] = index

        deps = _parse_dependencies(item, base, label, issues)
        parsed_deps[stage_id] = deps

        script_path = item.get("script_path")
        if not isinstance(script_path, str) or not script_path:
            issues.append(Issue("E_DAG", label, f"{base}.script_path", "missing_or_empty"))
            script_path = script_path if isinstance(script_path, str) else ""

        output_path = item.get("output_path")
        if not isinstance(output_path, str) or not output_path:
            issues.append(Issue("E_DAG", label, f"{base}.output_path", "missing_or_empty"))
            output_path = output_path if isinstance(output_path, str) else ""

        if not _is_positive_int(item.get("resource_units")):
            issues.append(
                Issue("E_DAG", label, f"{base}.resource_units", "must_be_positive_int")
            )
        resource_units = item.get("resource_units")
        if not _is_positive_int(resource_units):
            resource_units = 1

        if not _is_positive_int(item.get("estimated_ms")):
            issues.append(
                Issue("E_DAG", label, f"{base}.estimated_ms", "must_be_positive_int")
            )
        estimated_ms = item.get("estimated_ms")
        if not _is_positive_int(estimated_ms):
            estimated_ms = 1

        retries = item.get("retries", 0)
        if not _is_non_negative_int(retries):
            issues.append(
                Issue("E_DAG", label, f"{base}.retries", "must_be_non_negative_int")
            )
            retries = 0

        timeout_ms = item.get("timeout_ms")
        if timeout_ms is not None and not _is_positive_int(timeout_ms):
            issues.append(
                Issue("E_DAG", label, f"{base}.timeout_ms", "must_be_positive_int_or_null")
            )
            timeout_ms = None

        max_concurrency = item.get("max_concurrency")
        if max_concurrency is not None and not _is_positive_int(max_concurrency):
            issues.append(
                Issue(
                    "E_DAG",
                    label,
                    f"{base}.max_concurrency",
                    "must_be_positive_int_or_null",
                )
            )
            max_concurrency = None

        stages[stage_id] = Stage(
            stage_id=stage_id,
            script_path=script_path,
            dependencies=deps,
            resource_units=int(resource_units),
            estimated_ms=int(estimated_ms),
            retries=int(retries),
            output_path=output_path,
            timeout_ms=timeout_ms,
            max_concurrency=max_concurrency,
        )

    _validate_dependency_targets(stages, parsed_deps, issues)
    if not issues:
        _validate_acyclic(stages, issues)
    if not issues:
        _validate_schedulability(stages, int(budget), issues)

    if issues:
        sorted_issues = sorted(
            (issue.to_dict() for issue in issues),
            key=lambda item: (item["stage_id"], item["field"], item["reason"]),
        )
        raise DagValidationError(
            f"DAG validation failed with {len(sorted_issues)} issue(s)",
            sorted_issues,
        )

    return PipelineDag(
        dag_id=dag_id,
        dag_version=dag_version,
        stages=stages,
        global_resource_budget=int(budget),
        default_max_concurrency=int(default_conc),
        default_timeout_ms=default_timeout,
        source_path=source_path,
    )


def _parse_dependencies(
    item: dict, base: str, label: str, issues: List[Issue]
) -> List[Dependency]:
    deps_raw = item.get("dependencies", [])
    if not isinstance(deps_raw, list):
        issues.append(Issue("E_DAG", label, f"{base}.dependencies", "must_be_array"))
        return []
    deps: List[Dependency] = []
    seen: set = set()
    for dep_index, dep in enumerate(deps_raw):
        dep_base = f"{base}.dependencies[{dep_index}]"
        if not isinstance(dep, dict):
            issues.append(Issue("E_DAG", label, dep_base, "must_be_object"))
            continue
        target = dep.get("stage_id")
        kind = dep.get("kind", "strong")
        if not isinstance(target, str) or not target:
            issues.append(Issue("E_DAG", label, f"{dep_base}.stage_id", "missing_or_empty"))
            continue
        if kind not in VALID_DEP_KINDS:
            issues.append(
                Issue(
                    "E_DAG",
                    label,
                    f"{dep_base}.kind",
                    "invalid_kind",
                    {"allowed": list(VALID_DEP_KINDS), "actual": kind},
                )
            )
            continue
        key = (target, kind)
        if key in seen:
            issues.append(
                Issue("E_DAG", label, dep_base, "duplicate_dependency", {"target": target})
            )
            continue
        seen.add(key)
        deps.append(Dependency(stage_id=target, kind=kind))
    return deps


def _validate_dependency_targets(
    stages: Dict[str, Stage],
    parsed_deps: Dict[str, List[Dependency]],
    issues: List[Issue],
) -> None:
    known = set(stages)
    for stage_id, deps in parsed_deps.items():
        for dep in deps:
            if dep.stage_id == stage_id:
                issues.append(
                    Issue(
                        "E_DAG",
                        stage_id,
                        "dependencies",
                        "self_dependency",
                        {"target": dep.stage_id},
                    )
                )
            elif dep.stage_id not in known:
                issues.append(
                    Issue(
                        "E_DAG",
                        stage_id,
                        "dependencies",
                        "unknown_dependency_target",
                        {"target": dep.stage_id, "kind": dep.kind},
                    )
                )


def _validate_acyclic(stages: Dict[str, Stage], issues: List[Issue]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in stages}
    stack: List[str] = []

    def visit(node: str) -> Tuple[bool, List[str]]:
        color[node] = GRAY
        stack.append(node)
        for dep in sorted(stages[node].strong_deps + stages[node].weak_deps):
            if color[dep] == GRAY:
                cycle_start = stack.index(dep)
                return True, stack[cycle_start:] + [dep]
            if color[dep] == WHITE:
                cyclic, cycle = visit(dep)
                if cyclic:
                    return True, cycle
        stack.pop()
        color[node] = BLACK
        return False, []

    for sid in sorted(stages):
        if color[sid] == WHITE:
            cyclic, cycle = visit(sid)
            if cyclic:
                issues.append(
                    Issue(
                        "E_DAG",
                        cycle[0],
                        "dependencies",
                        "cycle_detected",
                        {"cycle": cycle},
                    )
                )
                return


def _validate_schedulability(
    stages: Dict[str, Stage], budget: int, issues: List[Issue]
) -> None:
    """A stage whose units exceed the global budget can never be scheduled."""
    for sid in sorted(stages):
        stage = stages[sid]
        if stage.resource_units > budget:
            issues.append(
                Issue(
                    "E_DAG",
                    sid,
                    "resource_units",
                    "exceeds_global_resource_budget",
                    {
                        "resource_units": stage.resource_units,
                        "global_resource_budget": budget,
                    },
                )
            )


def dag_fingerprint(dag: PipelineDag, base_dir: str) -> str:
    """Stable hash combining the canonical DAG body and every script's bytes.

    Used by replay to detect DAG version or script-content drift.
    """
    import hashlib

    digest = hashlib.sha256()
    canonical = json.dumps(
        dag.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    digest.update(b"dag:")
    digest.update(canonical)
    for sid in dag.stage_ids_sorted():
        script = dag.stages[sid].script_path
        script_abs = resolve_stage_script(dag, script)
        digest.update(f"|script:{sid}:".encode("utf-8"))
        if os.path.isfile(script_abs):
            with open(script_abs, "rb") as handle:
                digest.update(handle.read())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def resolve_stage_script(dag: PipelineDag, script_path: str) -> str:
    """Resolve a stage script relative to the DAG definition's directory."""
    if os.path.isabs(script_path):
        return os.path.normpath(script_path)
    anchor = dag.source_path or os.getcwd()
    dag_dir = os.path.dirname(anchor) if dag.source_path else os.getcwd()
    return os.path.normpath(os.path.join(dag_dir, script_path))
