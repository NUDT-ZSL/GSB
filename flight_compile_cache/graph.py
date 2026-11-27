"""Project parsing, graph validation, topological ordering, and graph diffs."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Mapping, Sequence

from .errors import (
    BusinessError,
    BusinessRuleError,
    E_GRAPH_CYCLE,
    E_GRAPH_DUPLICATE_NODE,
    E_GRAPH_OUTPUT_MISSING,
    E_GRAPH_REFERENCE_MISSING,
    E_GRAPH_SCHEMA,
    E_INVALID_DIMENSION,
    E_MODEL_NOT_FOUND,
    E_MODEL_VERSION_MISSING,
    E_OP_SHAPE_NOT_CLOSED,
    E_OP_UNKNOWN,
    E_PROJECT_SCHEMA,
    E_REQUEST_SCHEMA,
    E_STALE_VERSION,
    E_UNKNOWN_INPUT,
    E_VERSION_NOT_FOUND,
)
from .shapes import (
    bind_input_shapes,
    concrete_shape,
    infer_shape,
    shape_from_nested,
    validate_shape,
)
from .util import canonical_json, sha256_hex

SUPPORTED_OPS = frozenset(
    {
        "const",
        "matmul",
        "add",
        "sub",
        "mul",
        "div",
        "maximum",
        "minimum",
        "relu",
        "tanh",
        "sigmoid",
        "scale",
        "identity",
        "softmax",
        "reduce_sum",
        "reshape",
        "transpose",
        "concat",
        "slice",
    }
)

SHAPE_SENSITIVE_OPS = frozenset(
    {"matmul", "reduce_sum", "reshape", "transpose", "concat", "slice", "softmax"}
)


def project_error(message: str, *, path: str = "") -> BusinessError:
    return BusinessError(E_PROJECT_SCHEMA, message, path)


def get_model(project: Mapping[str, Any], model_id: str) -> Mapping[str, Any] | None:
    models = project.get("models")
    if not isinstance(models, Mapping):
        return None
    value = models.get(model_id)
    return value if isinstance(value, Mapping) else None


def get_version_spec(model: Mapping[str, Any], version: str) -> Mapping[str, Any] | None:
    versions = model.get("versions")
    if not isinstance(versions, Mapping):
        return None
    value = versions.get(version)
    return value if isinstance(value, Mapping) else None


def get_graph(model: Mapping[str, Any], version: str) -> Mapping[str, Any] | None:
    spec = get_version_spec(model, version)
    if spec is None:
        return None
    graph = spec.get("graph")
    return graph if isinstance(graph, Mapping) else None


def graph_fingerprint(graph: Mapping[str, Any]) -> str:
    return "graph_" + sha256_hex(
        {
            "inputs": graph.get("inputs", []),
            "nodes": graph.get("nodes", []),
            "outputs": graph.get("outputs", []),
        },
        length=24,
    )


def project_fingerprint(project: Mapping[str, Any]) -> str:
    models: dict[str, Any] = {}
    for model_id, model in sorted((project.get("models") or {}).items()):
        versions = {}
        for version, spec in sorted((model.get("versions") or {}).items()):
            graph = spec.get("graph", {})
            versions[version] = {
                "revision": spec.get("revision"),
                "graph_fingerprint": graph_fingerprint(graph),
            }
        models[model_id] = versions
    payload = {
        "schema_version": project.get("schema_version"),
        "project_id": project.get("project_id"),
        "models": models,
        "cache_config": project.get("cache_config"),
    }
    return "project_" + sha256_hex(payload, length=24)


def model_graph_fingerprints(project: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for model_id, model in sorted((project.get("models") or {}).items()):
        out[model_id] = {}
        for version, spec in sorted((model.get("versions") or {}).items()):
            out[model_id][version] = graph_fingerprint(spec.get("graph", {}))
    return out


def graph_inputs_by_name(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["name"]: item for item in graph.get("inputs", [])}


def nodes_by_id(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {node["id"]: node for node in graph.get("nodes", [])}


def topo_sort(graph: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[BusinessError]]:
    """Kahn topological sort; returns all errors encountered."""

    errors: list[BusinessError] = []
    nodes = graph.get("nodes", [])
    by_id: dict[str, Mapping[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            errors.append(BusinessError(E_GRAPH_SCHEMA, "node id must be a non-empty string", "graph.nodes"))
            continue
        if node_id in by_id:
            errors.append(
                BusinessError(E_GRAPH_DUPLICATE_NODE, f"duplicate node id {node_id!r}", f"graph.nodes.{node_id}")
            )
            continue
        by_id[node_id] = node
    input_names = {
        str(item["name"])
        for item in graph.get("inputs", [])
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    dependencies: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = {node_id: set() for node_id in by_id}
    for node_id, node in by_id.items():
        deps: set[str] = set()
        for reference in node.get("inputs", []) or []:
            if not isinstance(reference, str):
                errors.append(
                    BusinessError(
                        E_GRAPH_REFERENCE_MISSING,
                        f"node {node_id!r} has a non-string input reference",
                        f"graph.nodes.{node_id}.inputs",
                    )
                )
                continue
            if reference in input_names:
                continue
            if reference not in by_id:
                errors.append(
                    BusinessError(
                        E_GRAPH_REFERENCE_MISSING,
                        f"node {node_id!r} references missing tensor {reference!r}",
                        f"graph.nodes.{node_id}.inputs",
                    )
                )
                continue
            deps.add(reference)
            dependents.setdefault(reference, set()).add(node_id)
        dependencies[node_id] = deps

    indegree = {node_id: len(deps) for node_id, deps in dependencies.items()}
    ready = deque(node_id for node_id in by_id if indegree[node_id] == 0)
    ordered: list[Mapping[str, Any]] = []
    while ready:
        node_id = ready.popleft()
        ordered.append(by_id[node_id])
        for dependent in sorted(dependents.get(node_id, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) != len(by_id):
        remaining = sorted(node_id for node_id in by_id if indegree.get(node_id, 0) > 0)
        errors.append(
            BusinessError(
                E_GRAPH_CYCLE,
                f"graph contains a dependency cycle involving {remaining}",
                "graph.nodes",
            )
        )
    return ordered, errors


def validate_graph(graph: Any, *, path: str = "graph") -> dict[str, Any]:
    """Validate one graph and infer symbolic node shapes."""

    errors: list[BusinessError] = []
    if not isinstance(graph, Mapping):
        return {
            "ok": False,
            "errors": [BusinessError(E_GRAPH_SCHEMA, "graph must be an object", path).to_dict()],
            "topo_nodes": [],
            "input_names": [],
            "outputs": [],
            "node_shapes": {},
            "structural_digests": {},
        }
    inputs = graph.get("inputs")
    nodes = graph.get("nodes")
    outputs = graph.get("outputs")
    if not isinstance(inputs, list) or not inputs:
        errors.append(BusinessError(E_GRAPH_SCHEMA, "graph.inputs must be a non-empty list", f"{path}.inputs"))
        inputs = [] if not isinstance(inputs, list) else inputs
    if not isinstance(nodes, list) or not nodes:
        errors.append(BusinessError(E_GRAPH_SCHEMA, "graph.nodes must be a non-empty list", f"{path}.nodes"))
        nodes = [] if not isinstance(nodes, list) else nodes
    if not isinstance(outputs, list) or not outputs:
        errors.append(BusinessError(E_GRAPH_SCHEMA, "graph.outputs must be a non-empty list", f"{path}.outputs"))
        outputs = [] if not isinstance(outputs, list) else outputs

    input_names: list[str] = []
    declared_input_shapes: dict[str, tuple[Any, ...]] = {}
    for index, item in enumerate(inputs):
        item_path = f"{path}.inputs[{index}]"
        if not isinstance(item, Mapping):
            errors.append(BusinessError(E_GRAPH_SCHEMA, "graph input must be an object", item_path))
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            errors.append(BusinessError(E_GRAPH_SCHEMA, "graph input name must be a non-empty string", item_path))
            continue
        if name in declared_input_shapes:
            errors.append(BusinessError(E_GRAPH_SCHEMA, f"duplicate graph input {name!r}", item_path))
            continue
        try:
            declared = validate_shape(item.get("shape"), path=f"{item_path}.shape")
        except BusinessRuleError as exc:
            errors.append(exc.error)
            continue
        input_names.append(name)
        declared_input_shapes[name] = declared

    by_id: dict[str, Mapping[str, Any]] = {}
    for index, node in enumerate(nodes):
        node_path = f"{path}.nodes[{index}]"
        if not isinstance(node, Mapping):
            errors.append(BusinessError(E_GRAPH_SCHEMA, "node must be an object", node_path))
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            errors.append(BusinessError(E_GRAPH_SCHEMA, "node id must be a non-empty string", node_path))
            continue
        if node_id in by_id:
            errors.append(
                BusinessError(E_GRAPH_DUPLICATE_NODE, f"duplicate node id {node_id!r}", node_path)
            )
            continue
        if node_id in declared_input_shapes:
            errors.append(
                BusinessError(
                    E_GRAPH_DUPLICATE_NODE,
                    f"node id {node_id!r} collides with a graph input",
                    node_path,
                )
            )
            continue
        if node.get("op") not in SUPPORTED_OPS:
            errors.append(
                BusinessError(
                    E_OP_UNKNOWN,
                    f"unknown operator {node.get('op')!r}",
                    f"{node_path}.op",
                )
            )
        if not isinstance(node.get("inputs", []), list):
            errors.append(BusinessError(E_GRAPH_SCHEMA, "node.inputs must be a list", f"{node_path}.inputs"))
        if "attrs" in node and not isinstance(node.get("attrs"), Mapping):
            errors.append(BusinessError(E_GRAPH_SCHEMA, "node.attrs must be an object", f"{node_path}.attrs"))
        out_shape = node.get("out_shape")
        if node.get("op") == "const" and out_shape == []:
            pass
        else:
            try:
                validate_shape(out_shape, path=f"{node_path}.out_shape")
            except BusinessRuleError as exc:
                errors.append(exc.error)
        node_copy = dict(node)
        if not isinstance(node_copy.get("inputs"), list):
            node_copy["inputs"] = []
        if not isinstance(node_copy.get("attrs"), Mapping):
            node_copy["attrs"] = {}
        by_id[node_id] = node_copy

    ordered, topo_errors = topo_sort({"inputs": inputs, "nodes": list(by_id.values()), "outputs": outputs})
    errors.extend(topo_errors)

    # Symbolic shape closure.
    node_shapes: dict[str, tuple[Any, ...]] = {}
    for node in ordered:
        node_id = str(node["id"])
        node_path = f"{path}.nodes.{node_id}"
        input_shapes: list[tuple[Any, ...]] = []
        inputs_ok = True
        for reference in node.get("inputs", []):
            if reference in declared_input_shapes:
                input_shapes.append(declared_input_shapes[reference])
            elif reference in node_shapes:
                input_shapes.append(node_shapes[reference])
            else:
                inputs_ok = False
                break
        if not inputs_ok:
            continue
        try:
            inferred = infer_shape(
                str(node.get("op")),
                input_shapes,
                node.get("attrs", {}),
                path=node_path,
            )
        except BusinessRuleError as exc:
            errors.append(exc.error)
            continue
        declared = tuple(node.get("out_shape", []))
        if inferred != declared:
            errors.append(
                BusinessError(
                    E_OP_SHAPE_NOT_CLOSED,
                    f"node {node_id!r} declared {list(declared)} but inferred {list(inferred)}",
                    f"{node_path}.out_shape",
                    {"declared": list(declared), "inferred": list(inferred)},
                )
            )
        node_shapes[node_id] = inferred
        if node.get("op") == "const":
            try:
                value_shape, _ = shape_from_nested(node.get("attrs", {}).get("value"), path=f"{node_path}.attrs.value")
                if tuple(value_shape) != tuple(declared):
                    errors.append(
                        BusinessError(
                            E_OP_SHAPE_NOT_CLOSED,
                            f"const node {node_id!r} value shape {list(value_shape)} != declared {list(declared)}",
                            f"{node_path}.out_shape",
                        )
                    )
            except BusinessRuleError as exc:
                errors.append(exc.error)

    for output in outputs:
        if not isinstance(output, str):
            errors.append(BusinessError(E_GRAPH_OUTPUT_MISSING, "graph output must be a string", f"{path}.outputs"))
        elif output not in declared_input_shapes and output not in by_id:
            errors.append(
                BusinessError(
                    E_GRAPH_OUTPUT_MISSING,
                    f"graph output {output!r} is not defined",
                    f"{path}.outputs",
                )
            )

    structural_digests = {
        node_id: structural_node_digest(by_id, declared_input_shapes, node_id)
        for node_id in by_id
    }
    return {
        "ok": not errors,
        "errors": [error.to_dict() for error in errors],
        "topo_nodes": [node["id"] for node in ordered],
        "input_names": input_names,
        "declared_input_shapes": {name: list(shape) for name, shape in declared_input_shapes.items()},
        "outputs": list(outputs),
        "node_shapes": {key: list(value) for key, value in node_shapes.items()},
        "structural_digests": structural_digests,
    }


def structural_node_digest(
    by_id: Mapping[str, Mapping[str, Any]],
    graph_inputs: Mapping[str, Sequence[Any]],
    node_id: str,
    _cache: dict[str, str] | None = None,
    _visiting: set[str] | None = None,
) -> str:
    """Digest a node and its upstream structure, excluding concrete request shapes."""

    cache = _cache if _cache is not None else {}
    visiting = _visiting if _visiting is not None else set()
    if node_id in cache:
        return cache[node_id]
    if node_id in visiting:
        return "unit_cycle_" + sha256_hex({"node": node_id}, length=16)
    visiting.add(node_id)
    node = by_id[node_id]
    dependency_digests = []
    for reference in node.get("inputs", []) or []:
        if reference in by_id:
            dependency_digests.append(
                {
                    "node": reference,
                    "digest": structural_node_digest(
                        by_id, graph_inputs, reference, cache, visiting
                    ),
                }
            )
        else:
            dependency_digests.append(
                {"input": reference, "shape": list(graph_inputs.get(reference, []))}
            )
    payload = {
        "id": node_id,
        "op": node.get("op"),
        "out_shape": node.get("out_shape"),
        "attrs": node.get("attrs", {}),
        "dependencies": dependency_digests,
    }
    digest = "unit_" + sha256_hex(payload, length=24)
    visiting.discard(node_id)
    cache[node_id] = digest
    return digest


def validate_project(project: Any, *, validate_requests: bool = True) -> dict[str, Any]:
    """Validate project structure, all model versions, requests and events."""

    errors: list[BusinessError] = []
    if not isinstance(project, Mapping):
        return {
            "ok": False,
            "executable": False,
            "errors": [project_error("project must be a JSON object").to_dict()],
            "models": {},
            "requests": {},
            "events": {},
        }
    if not isinstance(project.get("schema_version"), str):
        errors.append(project_error("missing schema_version", path="schema_version"))
    models = project.get("models")
    if not isinstance(models, Mapping) or not models:
        errors.append(project_error("models must be a non-empty object", path="models"))
        models = {}
    cache_config = project.get("cache_config")
    if not isinstance(cache_config, Mapping):
        errors.append(project_error("cache_config must be an object", path="cache_config"))
        cache_config = {}
    else:
        capacity = cache_config.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            errors.append(project_error("cache_config.capacity must be an integer >= 1", path="cache_config.capacity"))
        tolerance = cache_config.get("numeric_tolerance", 1e-6)
        if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance < 0:
            errors.append(
                project_error("cache_config.numeric_tolerance must be non-negative", path="cache_config.numeric_tolerance")
            )

    model_reports: dict[str, Any] = {}
    for model_id, model in models.items():
        model_errors: list[BusinessError] = []
        if not isinstance(model, Mapping):
            model_reports[model_id] = {
                "ok": False,
                "errors": [project_error(f"model {model_id!r} must be an object", path=f"models.{model_id}").to_dict()],
                "versions": {},
            }
            continue
        versions = model.get("versions")
        if not isinstance(versions, Mapping) or not versions:
            model_errors.append(
                BusinessError(
                    E_MODEL_VERSION_MISSING,
                    f"model {model_id!r} has no versions",
                    f"models.{model_id}.versions",
                )
            )
            versions = {}
        version_reports: dict[str, Any] = {}
        for version, spec in versions.items():
            if not isinstance(spec, Mapping) or not isinstance(spec.get("graph"), Mapping):
                model_errors.append(
                    BusinessError(
                        E_MODEL_VERSION_MISSING,
                        f"version {version!r} of {model_id!r} lacks a graph",
                        f"models.{model_id}.versions.{version}",
                    )
                )
                version_reports[version] = {"ok": False, "errors": []}
                continue
            report = validate_graph(spec["graph"], path=f"models.{model_id}.versions.{version}.graph")
            version_reports[version] = report
            model_errors.extend(BusinessError(**item) for item in report["errors"])
        initial_version = model.get("initial_version")
        if not isinstance(initial_version, str) or initial_version not in versions:
            model_errors.append(
                BusinessError(
                    E_MODEL_VERSION_MISSING,
                    f"model {model_id!r} has invalid initial_version {initial_version!r}",
                    f"models.{model_id}.initial_version",
                )
            )
        manifest = model.get("version_manifest")
        if not isinstance(manifest, list) or not manifest:
            model_errors.append(
                BusinessError(
                    E_MODEL_VERSION_MISSING,
                    f"model {model_id!r} needs a version_manifest",
                    f"models.{model_id}.version_manifest",
                )
            )
        else:
            manifest_versions: set[str] = set()
            for index, entry in enumerate(manifest):
                entry_path = f"models.{model_id}.version_manifest[{index}]"
                if not isinstance(entry, Mapping):
                    model_errors.append(
                        BusinessError(
                            E_MODEL_VERSION_MISSING,
                            "version manifest entry must be an object",
                            entry_path,
                        )
                    )
                    continue
                version = entry.get("version")
                if not isinstance(version, str) or version not in versions:
                    model_errors.append(
                        BusinessError(
                            E_MODEL_VERSION_MISSING,
                            f"version manifest references unknown version {version!r}",
                            f"{entry_path}.version",
                        )
                    )
                    continue
                if version in manifest_versions:
                    model_errors.append(
                        BusinessError(
                            E_MODEL_VERSION_MISSING,
                            f"duplicate version manifest entry {version!r}",
                            f"{entry_path}.version",
                        )
                    )
                manifest_versions.add(version)
                expected_fp = entry.get("graph_fingerprint")
                if expected_fp is not None:
                    actual_fp = graph_fingerprint(versions[version].get("graph", {}))
                    if expected_fp != actual_fp:
                        model_errors.append(
                            BusinessError(
                                E_PROJECT_SCHEMA,
                                f"version {version!r} graph fingerprint does not match the graph",
                                f"{entry_path}.graph_fingerprint",
                                {"declared": expected_fp, "actual": actual_fp},
                            )
                        )
        model_reports[model_id] = {
            "ok": not model_errors,
            "errors": [error.to_dict() for error in model_errors],
            "versions": version_reports,
        }

    request_reports: dict[str, Any] = {}
    requests = project.get("requests", [])
    if not isinstance(requests, list):
        errors.append(project_error("requests must be a list", path="requests"))
        requests = []
    seen_request_ids: set[str] = set()
    for index, request in enumerate(requests):
        request_id = request.get("id") if isinstance(request, Mapping) else None
        report = validate_request_structure(
            project,
            request,
            path=f"requests[{index}]",
            seen_ids=seen_request_ids,
            check_graph_health=True,
        )
        key = request_id if isinstance(request_id, str) and request_id else f"#{index}"
        request_reports[key] = report

    event_reports: dict[str, Any] = {}
    events = project.get("events", [])
    if not isinstance(events, list):
        errors.append(project_error("events must be a list", path="events"))
        events = []
    seen_event_ids: set[str] = set()
    for index, event in enumerate(events):
        event_id = event.get("id") if isinstance(event, Mapping) else None
        event_errors: list[BusinessError] = []
        if not isinstance(event, Mapping):
            event_errors.append(project_error("event must be an object", path=f"events[{index}]"))
        else:
            eid = event.get("id")
            if not isinstance(eid, str) or not eid:
                event_errors.append(project_error("event id must be a non-empty string", path=f"events[{index}].id"))
            elif eid in seen_event_ids:
                event_errors.append(project_error(f"duplicate event id {eid!r}", path=f"events[{index}].id"))
            else:
                seen_event_ids.add(eid)
            if not isinstance(event.get("time"), (int, float)) or isinstance(event.get("time"), bool):
                event_errors.append(project_error("event time must be numeric", path=f"events[{index}].time"))
            if event.get("type") == "model_update":
                model_id = event.get("model_id")
                model = models.get(model_id) if isinstance(model_id, str) else None
                if not isinstance(model, Mapping):
                    event_errors.append(
                        BusinessError(
                            E_MODEL_NOT_FOUND,
                            f"event references unknown model {model_id!r}",
                            f"events[{index}].model_id",
                        )
                    )
                else:
                    for field in ("from_version", "to_version"):
                        version = event.get(field)
                        if not isinstance(version, str) or version not in (model.get("versions") or {}):
                            event_errors.append(
                                BusinessError(
                                    E_VERSION_NOT_FOUND,
                                    f"event {field} {version!r} not found for model {model_id!r}",
                                    f"events[{index}].{field}",
                                )
                            )
            else:
                event_errors.append(
                    BusinessError(
                        E_EVENT_INVALID,
                        f"unknown event type {event.get('type')!r}",
                        f"events[{index}].type",
                    )
                )
        key = event_id if isinstance(event_id, str) and event_id else f"#{index}"
        event_reports[key] = {
            "ok": not event_errors,
            "errors": [error.to_dict() for error in event_errors],
        }

    # Apply the same logical-time ordering as the runtime and flag requests that
    # still ask for a model version superseded by an earlier update.
    current_versions = {
        str(model_id): str(model["initial_version"])
        for model_id, model in models.items()
        if isinstance(model, Mapping) and isinstance(model.get("initial_version"), str)
    }
    timeline: list[tuple[float, int, str, Any]] = []
    for event in events:
        if isinstance(event, Mapping):
            timeline.append(
                (
                    float(event.get("time", 0)),
                    0,
                    str(event.get("id", "")),
                    ("event", event),
                )
            )
    for request in requests:
        if isinstance(request, Mapping):
            timeline.append(
                (
                    float(request.get("time", 0)),
                    1,
                    str(request.get("id", "")),
                    ("request", request),
                )
            )
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))
    for _, _, _, (kind, item) in timeline:
        if kind == "event":
            event_id = str(item.get("id", ""))
            event_report = event_reports.get(event_id, {})
            if event_report.get("ok") and item.get("type") == "model_update":
                current_versions[str(item.get("model_id"))] = str(item.get("to_version"))
            continue
        request_id = str(item.get("id", ""))
        report = request_reports.get(request_id)
        if report is None:
            continue
        model_id = item.get("model_id")
        version = item.get("model_version")
        current = current_versions.get(str(model_id))
        if (
            isinstance(version, str)
            and current is not None
            and version != current
            and "E_STALE_VERSION" not in [error["code"] for error in report.get("errors", [])]
        ):
            stale = BusinessError(
                E_STALE_VERSION,
                f"request uses stale version {version!r}; runtime version at this time is {current!r}",
                f"requests.{request_id}.model_version",
            )
            report.setdefault("errors", []).append(stale.to_dict())
            if report.get("error_code") is None:
                report["error_code"] = E_STALE_VERSION
            report["ok"] = False

    fatal_codes = {E_PROJECT_SCHEMA}
    executable = not any(error.code in fatal_codes for error in errors)
    executable = executable and all(report["ok"] for report in event_reports.values())
    # Invalid graphs are deliberately reported but only requests that reference
    # those graphs are skipped.  The project remains runnable.
    return {
        "ok": not errors and all(report["ok"] for report in model_reports.values()) and all(
            report["ok"] for report in request_reports.values()
        ) and all(report["ok"] for report in event_reports.values()),
        "executable": executable,
        "errors": [error.to_dict() for error in errors],
        "models": model_reports,
        "requests": request_reports,
        "events": event_reports,
    }


def validate_request_structure(
    project: Mapping[str, Any],
    request: Any,
    *,
    path: str,
    seen_ids: set[str] | None = None,
    check_graph_health: bool = True,
) -> dict[str, Any]:
    errors: list[BusinessError] = []
    if not isinstance(request, Mapping):
        return {
            "ok": False,
            "error_code": E_REQUEST_SCHEMA,
            "errors": [project_error("request must be an object", path=path).to_dict()],
        }
    request_id = request.get("id")
    if not isinstance(request_id, str) or not request_id:
        errors.append(project_error("request id must be a non-empty string", path=f"{path}.id"))
    elif seen_ids is not None:
        if request_id in seen_ids:
            errors.append(
                BusinessError(
                    E_REQUEST_DUPLICATE_ID,
                    f"duplicate request id {request_id!r}",
                    f"{path}.id",
                )
            )
        seen_ids.add(request_id)
    if not isinstance(request.get("time"), (int, float)) or isinstance(request.get("time"), bool):
        errors.append(project_error("request time must be numeric", path=f"{path}.time"))
    model_id = request.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        errors.append(project_error("request model_id must be a non-empty string", path=f"{path}.model_id"))
        model = None
    else:
        model = get_model(project, model_id)
        if model is None:
            errors.append(
                BusinessError(E_MODEL_NOT_FOUND, f"unknown model {model_id!r}", f"{path}.model_id")
            )
    version = request.get("model_version")
    if not isinstance(version, str) or not version:
        errors.append(project_error("request model_version must be a non-empty string", path=f"{path}.model_version"))
        version = ""
    if model is not None and version and get_version_spec(model, version) is None:
        errors.append(
            BusinessError(
                E_VERSION_NOT_FOUND,
                f"model {model_id!r} has no version {version!r}",
                f"{path}.model_version",
            )
        )
    shapes = request.get("input_shapes")
    if not isinstance(shapes, Mapping):
        errors.append(project_error("request input_shapes must be an object", path=f"{path}.input_shapes"))
    else:
        for name, shape in shapes.items():
            if not isinstance(name, str):
                errors.append(project_error("input name must be a string", path=f"{path}.input_shapes"))
                continue
            if not isinstance(shape, list):
                errors.append(
                    BusinessError(
                        E_REQUEST_SCHEMA,
                        f"input shape for {name!r} must be a list",
                        f"{path}.input_shapes.{name}",
                    )
                )
                continue
            for axis, dim in enumerate(shape):
                if isinstance(dim, bool) or not isinstance(dim, int):
                    errors.append(
                        BusinessError(
                            E_INVALID_DIMENSION,
                            f"input {name!r} axis {axis} must be an integer",
                            f"{path}.input_shapes.{name}[{axis}]",
                        )
                    )
                elif dim <= 0:
                    errors.append(
                        BusinessError(
                            E_INVALID_DIMENSION,
                            f"input {name!r} axis {axis} must be positive, got {dim}",
                            f"{path}.input_shapes.{name}[{axis}]",
                        )
                    )
    if check_graph_health and model is not None and version:
        graph = get_graph(model, version)
        if graph is not None:
            report = validate_graph(graph, path=f"models.{model_id}.versions.{version}.graph")
            if not report["ok"]:
                first = report["errors"][0]
                errors.append(
                    BusinessError(
                        first["code"],
                        f"request graph is invalid: {first['message']}",
                        f"{path}.model_version",
                        {"graph_errors": report["errors"]},
                    )
                )
            elif not errors and isinstance(shapes, Mapping):
                try:
                    plan_request_graph(project, request)
                except BusinessRuleError as exc:
                    errors.append(exc.error)
    if errors:
        return {
            "ok": False,
            "error_code": errors[0].code,
            "errors": [error.to_dict() for error in errors],
        }
    return {"ok": True, "error_code": None, "errors": []}


def plan_request_graph(
    project: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    current_versions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and resolve a request against a concrete version graph."""

    errors: list[BusinessError] = []
    model_id = request.get("model_id")
    version = request.get("model_version")
    if not isinstance(model_id, str):
        raise BusinessRuleError(E_REQUEST_SCHEMA, "model_id must be a string")
    model = get_model(project, model_id)
    if model is None:
        raise BusinessRuleError(E_MODEL_NOT_FOUND, f"unknown model {model_id!r}")
    if not isinstance(version, str) or get_version_spec(model, version) is None:
        raise BusinessRuleError(E_VERSION_NOT_FOUND, f"unknown version {version!r} for {model_id!r}")
    if current_versions is not None:
        current = current_versions.get(model_id)
        if current is not None and version != current:
            raise BusinessRuleError(
                E_STALE_VERSION,
                f"request uses stale version {version!r}; current version is {current!r}",
            )
    graph = get_graph(model, version)
    if graph is None:
        raise BusinessRuleError(E_MODEL_VERSION_MISSING, "model version has no graph")
    graph_report = validate_graph(graph, path=f"models.{model_id}.versions.{version}.graph")
    if not graph_report["ok"]:
        first = graph_report["errors"][0]
        raise BusinessRuleError(
            first["code"],
            f"model graph is invalid: {first['message']}",
            context={"graph_errors": graph_report["errors"]},
        )
    request_shapes = request.get("input_shapes")
    if not isinstance(request_shapes, Mapping):
        raise BusinessRuleError(E_REQUEST_SCHEMA, "input_shapes must be an object")
    declared_names = [item["name"] for item in graph["inputs"]]
    extra = sorted(set(request_shapes) - set(declared_names))
    if extra:
        raise BusinessRuleError(
            E_UNKNOWN_INPUT,
            f"request contains unknown inputs {extra}",
            context={"unknown_inputs": extra},
        )
    binding = bind_input_shapes(graph["inputs"], request_shapes)
    ordered, topo_errors = topo_sort(graph)
    if topo_errors:
        raise BusinessRuleError(topo_errors[0].code, topo_errors[0].message)
    node_input_shapes: dict[str, list[tuple[int, ...]]] = {}
    node_output_shapes: dict[str, tuple[int, ...]] = {}
    input_shapes_concrete: dict[str, tuple[int, ...]] = {}
    for item in graph["inputs"]:
        shape = concrete_shape(item["shape"], binding, path=f"graph.inputs.{item['name']}")
        input_shapes_concrete[item["name"]] = shape
    for node in ordered:
        node_id = str(node["id"])
        resolved_inputs = []
        for reference in node.get("inputs", []):
            if reference in input_shapes_concrete:
                resolved_inputs.append(input_shapes_concrete[reference])
            elif reference in node_output_shapes:
                resolved_inputs.append(node_output_shapes[reference])
            else:
                raise BusinessRuleError(
                    E_GRAPH_REFERENCE_MISSING,
                    f"node {node_id!r} references unresolved tensor {reference!r}",
                )
        node_input_shapes[node_id] = list(resolved_inputs)
        try:
            inferred = infer_shape(
                str(node.get("op")),
                resolved_inputs,
                node.get("attrs", {}),
                path=f"graph.nodes.{node_id}",
            )
            resolved = concrete_shape(inferred, binding, path=f"graph.nodes.{node_id}.out_shape")
        except BusinessRuleError as exc:
            errors.append(exc.error)
            continue
        declared = concrete_shape(node.get("out_shape", []), binding, path=f"graph.nodes.{node_id}.out_shape")
        if resolved != declared:
            errors.append(
                BusinessError(
                    E_OP_SHAPE_NOT_CLOSED,
                    f"node {node_id!r} declared {list(declared)} but request inference gives {list(resolved)}",
                    f"graph.nodes.{node_id}.out_shape",
                )
            )
        node_output_shapes[node_id] = resolved
    if errors:
        first = errors[0]
        raise BusinessRuleError(first.code, first.message, context={"errors": [e.to_dict() for e in errors]})

    output_names = list(graph["outputs"])
    output_shapes = {
        name: input_shapes_concrete[name] if name in input_shapes_concrete else node_output_shapes[name]
        for name in output_names
    }
    return {
        "model": model,
        "graph": graph,
        "version": version,
        "binding": binding,
        "input_shapes": input_shapes_concrete,
        "node_input_shapes": node_input_shapes,
        "node_output_shapes": node_output_shapes,
        "outputs": output_names,
        "output_shapes": output_shapes,
        "topo_nodes": [str(node["id"]) for node in ordered],
        "nodes_by_id": {str(node["id"]): node for node in ordered},
        "structural_digests": graph_report["structural_digests"],
    }


def shape_segment(graph: Mapping[str, Any], input_shapes: Mapping[str, Sequence[int]]) -> str:
    """A compact dynamic-shape label such as ``B=4,T=3``."""

    symbols: dict[str, int] = {}
    static_parts: list[str] = []
    for item in graph.get("inputs", []):
        name = item["name"]
        shape = input_shapes.get(name, [])
        for axis, (declared, actual) in enumerate(zip(item["shape"], shape)):
            if isinstance(declared, str):
                existing = symbols.get(declared)
                if existing is None:
                    symbols[declared] = int(actual)
            elif not isinstance(declared, int):
                static_parts.append(f"{name}[{axis}]={actual}")
    if symbols:
        return ",".join(f"{name}={symbols[name]}" for name in sorted(symbols))
    return ",".join(static_parts) if static_parts else "static"


def canonical_input_shapes(input_shapes: Mapping[str, Sequence[int]]) -> list[list[Any]]:
    return [[name, [int(dim) for dim in input_shapes[name]]] for name in sorted(input_shapes)]


def version_changed_units(
    project: Mapping[str, Any], model_id: str, from_version: str, to_version: str
) -> dict[str, Any]:
    """Compare two model graphs and return changed/affected/unaffected units."""

    model = get_model(project, model_id)
    if model is None:
        raise BusinessRuleError(E_MODEL_NOT_FOUND, f"unknown model {model_id!r}")
    old_graph = get_graph(model, from_version)
    new_graph = get_graph(model, to_version)
    if old_graph is None or new_graph is None:
        raise BusinessRuleError(E_VERSION_NOT_FOUND, "cannot diff missing model versions")
    old_nodes = nodes_by_id(old_graph)
    new_nodes = nodes_by_id(new_graph)
    all_nodes = sorted(set(old_nodes) | set(new_nodes))
    changed: list[str] = []
    for node_id in all_nodes:
        old = old_nodes.get(node_id)
        new = new_nodes.get(node_id)
        if old is None or new is None:
            changed.append(node_id)
            continue
        if canonical_json(_node_shape_digest_material(old)) != canonical_json(_node_shape_digest_material(new)):
            changed.append(node_id)
    if old_graph.get("inputs") != new_graph.get("inputs") or old_graph.get("outputs") != new_graph.get("outputs"):
        changed = all_nodes
    changed_set = set(changed)
    affected = set(changed)
    children: dict[str, set[str]] = {node_id: set() for node_id in all_nodes}
    for graph_nodes in (old_nodes, new_nodes):
        for node_id, node in graph_nodes.items():
            for reference in node.get("inputs", []):
                if reference in graph_nodes:
                    children.setdefault(reference, set()).add(node_id)
    queue = deque(changed)
    while queue:
        current = queue.popleft()
        for child in children.get(current, ()):
            if child not in affected:
                affected.add(child)
                queue.append(child)
    unaffected = sorted(set(new_nodes) - affected)
    return {
        "changed_units": sorted(changed_set),
        "affected_units": sorted(affected),
        "unaffected_units": unaffected,
        "added_units": sorted(set(new_nodes) - set(old_nodes)),
        "removed_units": sorted(set(old_nodes) - set(new_nodes)),
    }


def _node_shape_digest_material(node: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "op": node.get("op"),
        "inputs": node.get("inputs", []),
        "out_shape": node.get("out_shape"),
        "attrs": node.get("attrs", {}),
    }


def graph_structure_digest(graph: Mapping[str, Any]) -> str:
    return "structure_" + sha256_hex(
        {
            "inputs": graph.get("inputs", []),
            "nodes": [
                {
                    "id": node.get("id"),
                    "op": node.get("op"),
                    "inputs": node.get("inputs", []),
                    "out_shape": node.get("out_shape"),
                    "attrs": node.get("attrs", {}),
                }
                for node in graph.get("nodes", [])
            ],
            "outputs": graph.get("outputs", []),
        },
        length=24,
    )
