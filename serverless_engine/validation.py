"""Strict validation for manifests and call traces."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .errors import EngineError, bad
from .models import CallSpec, CallTrace, FunctionManifest, FunctionSpec, LayerManifest, LayerSpec
from .util import (
    as_nonnegative_int,
    as_positive_int,
    require_list,
    require_object,
    require_string,
)


_DIGEST_PREFIX = "sha256:"


def _validate_string_map(value: Any, path: str) -> dict[str, str]:
    obj = require_object(value, path, "E_BAD_LAYER")
    result: dict[str, str] = {}
    for key in sorted(obj):
        if not isinstance(key, str) or not key:
            raise bad("E_BAD_LAYER", "environment variable names must be non-empty", path)
        if not isinstance(obj[key], str):
            raise bad("E_BAD_LAYER", "environment variable values must be strings", f"{path}.{key}")
        result[key] = obj[key]
    return result


def parse_layer_manifest(payload: Any) -> LayerManifest:
    root = require_object(payload, "$", "E_BAD_LAYER")
    raw_layers = require_list(root.get("layers"), "$.layers", "E_BAD_LAYER")
    layers: dict[str, LayerSpec] = {}
    for index, raw in enumerate(raw_layers):
        path = f"$.layers[{index}]"
        item = require_object(raw, path, "E_BAD_LAYER")
        name = require_string(item.get("name"), f"{path}.name", "E_BAD_LAYER")
        if name in layers:
            raise bad("E_BAD_LAYER", f"duplicate layer name: {name}", f"{path}.name")
        digest = require_string(item.get("digest"), f"{path}.digest", "E_BAD_LAYER")
        if not digest.startswith(_DIGEST_PREFIX) or len(digest) <= len(_DIGEST_PREFIX):
            raise bad("E_BAD_LAYER", "digest must start with sha256:", f"{path}.digest")
        version = as_positive_int(item.get("version"), f"{path}.version", "E_BAD_LAYER")
        default_init_ms = as_nonnegative_int(
            item.get("default_init_ms", 0), f"{path}.default_init_ms", "E_BAD_LAYER"
        )
        tombstone = item.get("tombstone", False)
        if not isinstance(tombstone, bool):
            raise bad("E_BAD_LAYER", "tombstone must be boolean", f"{path}.tombstone")
        env = _validate_string_map(item.get("env", {}), f"{path}.env")
        depends_raw = require_list(item.get("depends_on", []), f"{path}.depends_on", "E_BAD_LAYER")
        depends: list[str] = []
        for dep_index, dependency in enumerate(depends_raw):
            dep_name = require_string(
                dependency, f"{path}.depends_on[{dep_index}]", "E_BAD_LAYER"
            )
            if dep_name in depends:
                raise bad("E_BAD_LAYER", f"duplicate layer dependency: {dep_name}", path)
            depends.append(dep_name)
        referenced_raw = require_list(
            item.get("referenced_by", []), f"{path}.referenced_by", "E_BAD_LAYER"
        )
        referenced_by: list[str] = []
        for ref_index, ref in enumerate(referenced_raw):
            ref_name = require_string(
                ref, f"{path}.referenced_by[{ref_index}]", "E_BAD_LAYER"
            )
            if ref_name in referenced_by:
                raise bad(
                    "E_BAD_LAYER", f"duplicate function reference: {ref_name}", path
                )
            referenced_by.append(ref_name)
        layers[name] = LayerSpec(
            name=name,
            digest=digest,
            version=version,
            default_init_ms=default_init_ms,
            tombstone=tombstone,
            env=env,
            depends_on=tuple(sorted(depends)),
            referenced_by=tuple(sorted(referenced_by)),
        )

    for layer in layers.values():
        for dependency in layer.depends_on:
            if dependency not in layers:
                raise bad(
                    "E_LAYER_CLOSURE",
                    f"layer {layer.name} depends on missing layer {dependency}",
                    f"layer:{layer.name}",
                )

    visit: dict[str, int] = {}
    stack: list[str] = []

    def walk(name: str) -> None:
        state = visit.get(name, 0)
        if state == 2:
            return
        if state == 1:
            start = stack.index(name)
            cycle = stack[start:] + [name]
            raise bad(
                "E_LAYER_CYCLE",
                "layer dependency cycle: " + " -> ".join(cycle),
                f"layer:{name}",
            )
        visit[name] = 1
        stack.append(name)
        for dependency in layers[name].depends_on:
            walk(dependency)
        stack.pop()
        visit[name] = 2

    for name in sorted(layers):
        walk(name)

    return LayerManifest(layers=layers)


def parse_function_manifest(payload: Any, layers: LayerManifest) -> FunctionManifest:
    root = require_object(payload, "$", "E_BAD_FUNC")
    budget = as_positive_int(
        root.get("global_concurrency_budget"),
        "$.global_concurrency_budget",
        "E_BAD_FUNC",
    )
    raw_functions = require_list(root.get("functions"), "$.functions", "E_BAD_FUNC")
    functions: dict[str, FunctionSpec] = {}
    reverse_refs: dict[str, set[str]] = {name: set() for name in layers.layers}

    for index, raw in enumerate(raw_functions):
        path = f"$.functions[{index}]"
        item = require_object(raw, path, "E_BAD_FUNC")
        name = require_string(item.get("name"), f"{path}.name", "E_BAD_FUNC")
        if name in functions:
            raise bad("E_BAD_FUNC", f"duplicate function name: {name}", f"{path}.name")
        runtime = require_string(item.get("runtime"), f"{path}.runtime", "E_BAD_FUNC")
        refs_raw = require_list(item.get("layer_refs"), f"{path}.layer_refs", "E_BAD_FUNC")
        refs: list[str] = []
        for ref_index, ref in enumerate(refs_raw):
            ref_name = require_string(
                ref, f"{path}.layer_refs[{ref_index}]", "E_BAD_FUNC"
            )
            if ref_name in refs:
                raise bad("E_BAD_FUNC", f"duplicate layer reference: {ref_name}", path)
            if ref_name not in layers.layers:
                raise bad(
                    "E_LAYER_CLOSURE",
                    f"function {name} references missing layer {ref_name}",
                    f"{path}.layer_refs[{ref_index}]",
                )
            refs.append(ref_name)
            reverse_refs[ref_name].add(name)

        params_raw = require_object(
            item.get("layer_params", {}), f"{path}.layer_params", "E_BAD_FUNC"
        )
        if set(params_raw) != set(refs):
            missing = sorted(set(refs) - set(params_raw))
            extra = sorted(set(params_raw) - set(refs))
            raise bad(
                "E_BAD_FUNC",
                f"layer_params must exactly cover layer_refs (missing={missing}, extra={extra})",
                f"{path}.layer_params",
            )
        layer_params: dict[str, dict[str, Any]] = {}
        for ref in refs:
            param_path = f"{path}.layer_params.{ref}"
            param_raw = require_object(params_raw[ref], param_path, "E_BAD_FUNC")
            env = _validate_string_map(param_raw.get("env", {}), f"{param_path}.env")
            init_ms = as_nonnegative_int(
                param_raw.get(
                    "init_logical_ms", layers.layers[ref].default_init_ms
                ),
                f"{param_path}.init_logical_ms",
                "E_BAD_FUNC",
            )
            layer_params[ref] = {"env": env, "init_logical_ms": init_ms}

        memory_mib = as_positive_int(item.get("memory_mib"), f"{path}.memory_mib", "E_BAD_FUNC")
        max_concurrency = as_positive_int(
            item.get("max_concurrency"), f"{path}.max_concurrency", "E_BAD_FUNC"
        )
        warm_pool_target = as_nonnegative_int(
            item.get("warm_pool_target", 0), f"{path}.warm_pool_target", "E_BAD_FUNC"
        )
        warm_ttl_ms = as_positive_int(
            item.get("warm_ttl_ms"), f"{path}.warm_ttl_ms", "E_BAD_FUNC"
        )
        init_base_ms = as_nonnegative_int(
            item.get("init_base_ms", 0), f"{path}.init_base_ms", "E_BAD_FUNC"
        )
        functions[name] = FunctionSpec(
            name=name,
            runtime=runtime,
            layer_refs=tuple(refs),
            layer_params=layer_params,
            memory_mib=memory_mib,
            max_concurrency=max_concurrency,
            warm_pool_target=warm_pool_target,
            warm_ttl_ms=warm_ttl_ms,
            init_base_ms=init_base_ms,
        )

    for layer in layers.layers.values():
        declared = set(layer.referenced_by)
        actual = reverse_refs[layer.name]
        if declared != actual:
            raise bad(
                "E_BAD_LAYER",
                f"referenced_by mismatch for {layer.name}: declared={sorted(declared)}, actual={sorted(actual)}",
                f"layer:{layer.name}.referenced_by",
            )

    return FunctionManifest(global_concurrency_budget=budget, functions=functions)


def effective_layers(
    function: FunctionSpec, layers: dict[str, LayerSpec]
) -> tuple[str, ...]:
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for dependency in layers[name].depends_on:
            visit(dependency)

    for ref in function.layer_refs:
        visit(ref)
    return tuple(sorted(seen))


def layer_closure(
    changed: Iterable[str], layers: dict[str, LayerSpec]
) -> set[str]:
    closure = set(changed)
    changed_list = sorted(closure)
    while changed_list:
        current = changed_list.pop()
        for layer in layers.values():
            if current in layer.depends_on and layer.name not in closure:
                closure.add(layer.name)
                changed_list.append(layer.name)
    return closure


def affected_functions(
    changed_layers: Iterable[str],
    functions: dict[str, FunctionSpec],
    layers: dict[str, LayerSpec],
) -> tuple[str, ...]:
    closure = layer_closure(changed_layers, layers)
    affected = []
    for function in functions.values():
        if set(effective_layers(function, layers)) & closure:
            affected.append(function.name)
    return tuple(sorted(affected))


def parse_trace(payload: Any, manifest: FunctionManifest) -> CallTrace:
    root = require_object(payload, "$", "E_BAD_CALL")
    raw_calls = require_list(root.get("calls"), "$.calls", "E_BAD_CALL")
    calls: list[CallSpec] = []
    last_seq = 0
    for index, raw in enumerate(raw_calls):
        path = f"$.calls[{index}]"
        item = require_object(raw, path, "E_BAD_CALL")
        seq = as_positive_int(item.get("seq"), f"{path}.seq", "E_BAD_CALL")
        if seq <= last_seq:
            raise bad(
                "E_BAD_CALL",
                f"call seq must be strictly increasing: {seq} after {last_seq}",
                f"{path}.seq",
            )
        last_seq = seq
        function_name = require_string(item.get("function"), f"{path}.function", "E_BAD_CALL")
        if function_name not in manifest.functions:
            raise bad(
                "E_BAD_CALL",
                f"call references unknown function: {function_name}",
                f"{path}.function",
            )
        function = manifest.functions[function_name]
        arrival_ms = as_nonnegative_int(
            item.get("arrival_clock_ms"), f"{path}.arrival_clock_ms", "E_BAD_CALL"
        )
        occupancy = as_positive_int(
            item.get("occupancy"), f"{path}.occupancy", "E_BAD_CALL"
        )
        if occupancy > function.max_concurrency:
            raise bad(
                "E_BAD_CALL",
                f"occupancy {occupancy} exceeds max_concurrency {function.max_concurrency}",
                f"{path}.occupancy",
            )
        duration_ms = as_positive_int(
            item.get("duration_ms", 25), f"{path}.duration_ms", "E_BAD_CALL"
        )
        wait_budget_ms = as_nonnegative_int(
            item.get("wait_budget_ms", 0), f"{path}.wait_budget_ms", "E_BAD_CALL"
        )
        cancel_after_ms = item.get("cancel_after_ms")
        if cancel_after_ms is not None:
            cancel_after_ms = as_nonnegative_int(
                cancel_after_ms, f"{path}.cancel_after_ms", "E_BAD_CALL"
            )
        calls.append(
            CallSpec(
                seq=seq,
                function=function_name,
                arrival_ms=arrival_ms,
                occupancy=occupancy,
                duration_ms=duration_ms,
                wait_budget_ms=wait_budget_ms,
                cancel_after_ms=cancel_after_ms,
            )
        )
    return CallTrace(calls=tuple(calls))


def manifest_payload(
    function_manifest: FunctionManifest, layer_manifest: LayerManifest
) -> dict[str, Any]:
    functions = []
    for name in sorted(function_manifest.functions):
        function = function_manifest.functions[name]
        functions.append(
            {
                "name": function.name,
                "runtime": function.runtime,
                "layer_refs": list(function.layer_refs),
                "layer_params": {
                    key: {
                        "env": dict(function.layer_params[key]["env"]),
                        "init_logical_ms": function.layer_params[key]["init_logical_ms"],
                    }
                    for key in function.layer_refs
                },
                "memory_mib": function.memory_mib,
                "max_concurrency": function.max_concurrency,
                "warm_pool_target": function.warm_pool_target,
                "warm_ttl_ms": function.warm_ttl_ms,
                "init_base_ms": function.init_base_ms,
            }
        )
    layers = []
    for name in sorted(layer_manifest.layers):
        layer = layer_manifest.layers[name]
        layers.append(
            {
                "name": layer.name,
                "digest": layer.digest,
                "version": layer.version,
                "default_init_ms": layer.default_init_ms,
                "tombstone": layer.tombstone,
                "env": dict(layer.env),
                "depends_on": list(layer.depends_on),
                "referenced_by": list(layer.referenced_by),
            }
        )
    return {
        "global_concurrency_budget": function_manifest.global_concurrency_budget,
        "functions": functions,
        "layers": layers,
    }
