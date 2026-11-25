from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .cachejudge import make_cache_key, resolve_build_values
from .errors import LayerForgeError
from .layers import build_layer_spec, encode_layer_object, estimate_cost_ms


SUBST_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def normalize_dep_id(ref: str) -> str:
    if not isinstance(ref, str) or not ref:
        raise LayerForgeError("dependency reference must be a non-empty string")
    return ref.split(":", 1)[1] if ref.startswith("step:") else ref


def dependency_ids(step: Mapping[str, Any]) -> List[str]:
    deps: List[str] = []
    base_ref = step.get("base_ref") or step.get("base")
    if isinstance(base_ref, str) and _is_step_ref(base_ref):
        deps.append(normalize_dep_id(base_ref))
    for ref in step.get("depends_on") or []:
        dep_id = normalize_dep_id(ref)
        if dep_id not in deps:
            deps.append(dep_id)
    return deps


def _is_step_ref(ref: str) -> bool:
    return ref.startswith("step:") or not ref.startswith("base:")


def topological_steps(steps: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    by_id = {step["step_id"]: step for step in steps}
    deps = {
        step["step_id"]: [dep for dep in dependency_ids(step) if dep in by_id]
        for step in steps
    }
    remaining = dict(deps)
    completed: Set[str] = set()
    result: List[Mapping[str, Any]] = []
    while remaining:
        ready = [
            sid
            for sid, pending in remaining.items()
            if all(dep in completed for dep in pending)
        ]
        if not ready:
            raise LayerForgeError(
                "dependency cycle detected",
                details={"remaining": remaining},
            )
        ready.sort(key=lambda sid: (int(by_id[sid]["seq"]), sid))
        chosen = ready[0]
        result.append(by_id[chosen])
        completed.add(chosen)
        del remaining[chosen]
    return result


def downstream_map(steps: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    by_id = {step["step_id"]: step for step in steps}
    downstream: Dict[str, Set[str]] = {sid: set() for sid in by_id}
    for step in steps:
        sid = step["step_id"]
        for dep in dependency_ids(step):
            if dep in by_id:
                downstream.setdefault(dep, set()).add(sid)
    return {sid: sorted(children) for sid, children in downstream.items()}


def closure_downstream(root_ids: Sequence[str], steps: Sequence[Mapping[str, Any]]) -> List[str]:
    downstream = downstream_map(steps)
    seen: Set[str] = set()
    stack = list(root_ids)
    while stack:
        current = stack.pop()
        for child in downstream.get(current, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    ordered = topological_steps(steps)
    return [step["step_id"] for step in ordered if step["step_id"] in set(root_ids) | seen]


def substitute_scalar(value: Any, params: Mapping[str, Any], *, field: str) -> Any:
    if not isinstance(value, str):
        return value

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            raise LayerForgeError(
                "build parameter reference cannot be resolved",
                details={"field": field, "parameter": name, "available": sorted(params)},
            )
        return str(params[name])

    return SUBST_RE.sub(repl, value)


def effective_parameters(
    bundle: Mapping[str, Any],
    overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    params: Dict[str, Any] = dict((bundle.get("build_params") or {}).get("args") or {})
    params.update(overrides or {})
    return {key: params[key] for key in sorted(params)}


def changed_parameters(
    bundle: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    original = (bundle.get("build_params") or {}).get("args") or {}
    result = []
    for key in sorted(overrides):
        old = original.get(key)
        new = overrides[key]
        if old != new:
            result.append({"name": key, "old_value": old, "new_value": new})
    return result


def direct_parameter_steps(
    steps: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
) -> Dict[str, List[str]]:
    changed_names = {change["name"] for change in changes}
    result: Dict[str, List[str]] = {name: [] for name in changed_names}
    for step in steps:
        refs = set(step.get("arg_refs") or [])
        raw_blob_parts: List[str] = []
        for value in (step.get("args") or {}).values():
            if isinstance(value, str):
                raw_blob_parts.append(value)
        for value in (step.get("env") or {}).values():
            if isinstance(value, str):
                raw_blob_parts.append(value)
        blob = "\n".join(raw_blob_parts)
        referenced = {match.group(1) for match in SUBST_RE.finditer(blob)}
        for name in changed_names:
            if name in refs or name in referenced:
                result[name].append(step["step_id"])
    for name in result:
        result[name].sort()
    return result


def _base_file_metadata(base_layer: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    files = base_layer.get("files") or {}
    result = {}
    for path, meta in files.items():
        if isinstance(meta, Mapping) and "sha256" in meta:
            result[path] = {
                "sha256": meta["sha256"],
                "size": int(meta.get("size", 0)),
                "mode": meta.get("mode", "0644"),
            }
        elif isinstance(meta, Mapping) and "content" in meta:
            content = str(meta["content"]).encode("utf-8")
            from .canonical import byte_digest

            result[path] = {
                "sha256": byte_digest(content),
                "size": len(content),
                "mode": meta.get("mode", "0644"),
            }
    return dict(sorted(result.items()))


def derive_plan(
    bundle: Mapping[str, Any],
    *,
    parameter_overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    steps = topological_steps(bundle["steps"])
    base_layers = bundle["base_layers"]
    params = effective_parameters(bundle, parameter_overrides)
    algorithm = bundle.get("config", {}).get("hash_algorithm", "sha256")
    derived: Dict[str, Dict[str, Any]] = {}
    planned_steps: List[Dict[str, Any]] = []

    for step in steps:
        step_id = step["step_id"]
        base_ref = step.get("base_ref") or step.get("base")
        if base_ref in base_layers:
            parent_digest = base_layers[base_ref]["digest"]
            parent_files = _base_file_metadata(base_layers[base_ref])
            parent_step_id = None
        else:
            parent_id = normalize_dep_id(base_ref)
            parent = derived[parent_id]
            parent_digest = parent["layer_digest"]
            parent_files = parent["spec"]["result_files"]
            parent_step_id = parent_id

        dependency_digests = []
        for ref in step.get("depends_on") or []:
            dep_id = normalize_dep_id(ref)
            if dep_id == parent_step_id:
                continue
            dependency_digests.append(
                {"step_id": dep_id, "layer_digest": derived[dep_id]["layer_digest"]}
            )
        dependency_digests.sort(key=lambda item: item["step_id"])

        raw_args = dict(step.get("args") or {})
        resolved_args: Dict[str, Any] = {}
        for key, value in raw_args.items():
            resolved_args[key] = substitute_scalar(
                value, params, field=f"step.{step_id}.args.{key}"
            )
        for ref in step.get("arg_refs") or []:
            if ref in params and ref not in resolved_args:
                resolved_args[ref] = params[ref]
        resolved_env: Dict[str, Any] = {}
        substitution_params = {**params, **resolved_args}
        for key, value in (step.get("env") or {}).items():
            resolved_env[key] = substitute_scalar(
                value, substitution_params, field=f"step.{step_id}.env.{key}"
            )

        planned_step_view = {
            **step,
            "args": resolved_args,
            "env": resolved_env,
        }
        spec = build_layer_spec(
            planned_step_view,
            actions=step["actions"],
            parent_digest=parent_digest,
            parent_ref=base_ref,
            written_at_ms=int(step["written_at_ms"]),
            parent_files=parent_files,
            dependency_digests=dependency_digests,
        )
        object_bytes, layer_digest = encode_layer_object(spec)
        cache_key = make_cache_key(
            step=planned_step_view,
            layer_digest=layer_digest,
            parent_digest=parent_digest,
            args=resolved_args,
            env=resolved_env,
            dependency_digests=dependency_digests,
            algorithm=algorithm,
        )
        item = {
            "step_id": step_id,
            "seq": int(step["seq"]),
            "step": step,
            "parent_ref": base_ref,
            "parent_step_id": parent_step_id,
            "parent_digest": parent_digest,
            "dependency_ids": dependency_ids(step),
            "dependency_digests": dependency_digests,
            "resolved_args": dict(sorted(resolved_args.items())),
            "resolved_env": dict(sorted(resolved_env.items())),
            "spec": spec,
            "object_bytes": object_bytes,
            "layer_digest": layer_digest,
            "cache_key": cache_key,
            "cost_ms": estimate_cost_ms(step),
        }
        derived[step_id] = item
        planned_steps.append(item)

    changes = changed_parameters(bundle, parameter_overrides or {})
    direct = direct_parameter_steps(bundle["steps"], changes)
    roots = sorted({sid for sids in direct.values() for sid in sids})
    invalid_set = closure_downstream(roots, bundle["steps"]) if roots else []
    return {
        "steps": planned_steps,
        "by_id": derived,
        "parameter_overrides": dict(parameter_overrides or {}),
        "parameter_changes": changes,
        "direct_parameter_steps": direct,
        "invalid_closure": invalid_set,
        "invalid_roots": roots,
    }
