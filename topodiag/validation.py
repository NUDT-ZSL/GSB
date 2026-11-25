"""Input validation with explicit business error codes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, require
from .model import (
    InstantiationRequest,
    NamespaceTopology,
    ServiceTemplate,
    Topology,
)
from .util import (
    INSTANCE_RE,
    RESOURCE_RE,
    TEMPLATE_RE,
    is_nonnegative_int,
    is_positive_int,
    parse_semver,
    read_json,
    validate_labels,
)


ALLOWED_DESIRED_STATES = {"active", "paused", "abandoned"}
CLOCK_TOLERANCE_MS = 100


def _as_dict(value: Any, code: str, message: str, **diagnostics: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, message, **diagnostics)
    return value


def _as_list(value: Any, code: str, message: str, **diagnostics: Any) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(code, message, **diagnostics)
    return value


def _validate_dependency_graph(
    graph: dict[str, tuple[str, ...]],
    *,
    source: str,
    version: str | int | None,
) -> None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        current = state.get(name, 0)
        if current == 1:
            cycle_start = stack.index(name) if name in stack else 0
            cycle = stack[cycle_start:] + [name]
            raise BusinessError(
                "DEPENDENCY_CYCLE",
                f"{source} dependency graph contains a cycle: {' -> '.join(cycle)}",
                template_name=name,
                version=version,
                cycle=cycle,
            )
        if current == 2:
            return
        state[name] = 1
        stack.append(name)
        for dependency in graph[name]:
            if dependency not in graph:
                raise BusinessError(
                    "MISSING_DEPENDENCY",
                    f"{source} template {name!r} depends on missing template {dependency!r}",
                    template_name=name,
                    version=version,
                    missing_dependency=dependency,
                )
            visit(dependency)
        stack.pop()
        state[name] = 2

    for node in sorted(graph):
        visit(node)


def validate_template_manifest(payload: Any) -> tuple[str, int, dict[str, ServiceTemplate]]:
    data = _as_dict(
        payload,
        "INVALID_TEMPLATE_MANIFEST",
        "Template manifest must be a JSON object",
    )
    require(
        data.get("format") == "service-template-manifest/v1",
        "INVALID_TEMPLATE_MANIFEST",
        "Unsupported template manifest format",
        version=data.get("manifest_version"),
        actual_format=data.get("format"),
    )
    manifest_version = data.get("manifest_version")
    parse_semver(manifest_version)
    injected_clock = data.get("injected_logical_clock_ms")
    require(
        is_nonnegative_int(injected_clock),
        "INVALID_LOGICAL_CLOCK",
        "Injected logical clock must be a non-negative integer",
        version=manifest_version,
        logical_clock=injected_clock,
    )
    raw_templates = _as_list(
        data.get("templates"),
        "INVALID_TEMPLATE_MANIFEST",
        "templates must be an array",
        version=manifest_version,
    )
    templates: dict[str, ServiceTemplate] = {}
    for index, raw in enumerate(raw_templates):
        item = _as_dict(
            raw,
            "INVALID_TEMPLATE_MANIFEST",
            f"Template entry {index} must be an object",
            version=manifest_version,
        )
        name = item.get("name")
        if not isinstance(name, str) or not TEMPLATE_RE.fullmatch(name):
            raise BusinessError(
                "INVALID_TEMPLATE_NAME",
                f"Invalid template name: {name!r}",
                template_name=name,
                version=item.get("version"),
            )
        require(
            name not in templates,
            "TEMPLATE_NAME_DUPLICATE",
            f"Duplicate template name: {name}",
            template_name=name,
            version=item.get("version"),
        )
        version = item.get("version")
        require(
            is_positive_int(version),
            "INVALID_TEMPLATE_VERSION",
            f"Template {name} version must be a positive integer",
            template_name=name,
            version=version,
        )
        logical_clock = item.get("logical_clock")
        require(
            is_nonnegative_int(logical_clock),
            "INVALID_LOGICAL_CLOCK",
            f"Template {name} logical clock must be non-negative",
            template_name=name,
            version=version,
            logical_clock=logical_clock,
        )
        dependencies = _as_list(
            item.get("dependencies"),
            "INVALID_TEMPLATE_MANIFEST",
            f"Template {name} dependencies must be an array",
            template_name=name,
            version=version,
        )
        dependency_names: list[str] = []
        for dependency in dependencies:
            if not isinstance(dependency, str) or not TEMPLATE_RE.fullmatch(dependency):
                raise BusinessError(
                    "INVALID_DEPENDENCY",
                    f"Template {name} has invalid dependency {dependency!r}",
                    template_name=name,
                    version=version,
                    dependency=dependency,
                )
            dependency_names.append(dependency)
        if len(set(dependency_names)) != len(dependency_names):
            raise BusinessError(
                "INVALID_DEPENDENCY",
                f"Template {name} has duplicate dependencies",
                template_name=name,
                version=version,
            )
        labels = validate_labels(
            item.get("labels"),
            template_name=name,
            version=version,
        )
        resources_raw = _as_dict(
            item.get("resource_requirements"),
            "INVALID_RESOURCE_REQUIREMENT",
            f"Template {name} resource_requirements must be an object",
            template_name=name,
            version=version,
        )
        resources: dict[str, int] = {}
        for resource_name, amount in resources_raw.items():
            if not isinstance(resource_name, str) or not RESOURCE_RE.fullmatch(resource_name):
                raise BusinessError(
                    "INVALID_RESOURCE_REQUIREMENT",
                    f"Template {name} has invalid resource name {resource_name!r}",
                    template_name=name,
                    version=version,
                    resource_name=resource_name,
                )
            require(
                is_positive_int(amount),
                "INVALID_RESOURCE_REQUIREMENT",
                f"Template {name} resource {resource_name} must be a positive integer",
                template_name=name,
                version=version,
                resource_name=resource_name,
                amount=amount,
            )
            resources[resource_name] = amount
        require(
            bool(resources),
            "INVALID_RESOURCE_REQUIREMENT",
            f"Template {name} must declare at least one resource",
            template_name=name,
            version=version,
        )
        templates[name] = ServiceTemplate(
            name=name,
            version=version,
            dependencies=tuple(sorted(dependency_names)),
            labels=labels,
            logical_clock=logical_clock,
            resources=tuple(sorted(resources.items())),
        )
    graph = {name: template.dependencies for name, template in templates.items()}
    _validate_dependency_graph(graph, source="service template", version=manifest_version)
    return manifest_version, injected_clock, templates


def validate_requests(
    payload: Any,
    *,
    templates: dict[str, ServiceTemplate],
    clock_tolerance_ms: int = CLOCK_TOLERANCE_MS,
) -> list[InstantiationRequest]:
    data = _as_dict(
        payload,
        "INVALID_REQUEST_STREAM",
        "Request stream must be a JSON object",
    )
    require(
        data.get("format") == "instantiation-request-stream/v1",
        "INVALID_REQUEST_STREAM",
        "Unsupported request stream format",
        actual_format=data.get("format"),
    )
    raw_requests = _as_list(
        data.get("requests"),
        "INVALID_REQUEST_STREAM",
        "requests must be an array",
    )
    requests: list[InstantiationRequest] = []
    seen_seq: set[int] = set()
    seen_instance: set[str] = set()
    previous_clock = -1
    for raw in raw_requests:
        item = _as_dict(
            raw,
            "INVALID_REQUEST_STREAM",
            "Each request must be an object",
        )
        seq = item.get("request_seq")
        require(
            is_positive_int(seq),
            "INVALID_REQUEST_SEQUENCE",
            "request_seq must be a positive integer",
            request_seq=seq,
            version=item.get("template_name"),
        )
        require(
            seq not in seen_seq,
            "REQUEST_SEQ_DUPLICATE",
            f"Duplicate request_seq: {seq}",
            request_seq=seq,
            version=item.get("template_name"),
        )
        seen_seq.add(seq)
        template_name = item.get("template_name")
        if not isinstance(template_name, str) or not TEMPLATE_RE.fullmatch(template_name):
            raise BusinessError(
                "INVALID_REQUEST_STREAM",
                f"Request {seq} has invalid template_name",
                request_seq=seq,
                template_name=template_name,
            )
        if template_name not in templates:
            raise BusinessError(
                "REQUEST_UNKNOWN_TEMPLATE",
                f"Request {seq} references unknown template {template_name!r}",
                request_seq=seq,
                template_name=template_name,
                version=None,
            )
        namespace = item.get("target_topology_namespace")
        if not isinstance(namespace, str) or not INSTANCE_RE.fullmatch(namespace):
            raise BusinessError(
                "INVALID_REQUEST_STREAM",
                f"Request {seq} has invalid target_topology_namespace",
                request_seq=seq,
                template_name=template_name,
                namespace=namespace,
            )
        instance_id = item.get("instance_id")
        if not isinstance(instance_id, str) or not INSTANCE_RE.fullmatch(instance_id):
            raise BusinessError(
                "INVALID_INSTANCE_ID",
                f"Request {seq} has invalid instance_id",
                request_seq=seq,
                template_name=template_name,
                instance_id=instance_id,
            )
        require(
            instance_id not in seen_instance,
            "INSTANCE_ID_DUPLICATE",
            f"Duplicate instance_id: {instance_id}",
            request_seq=seq,
            template_name=template_name,
            instance_id=instance_id,
        )
        seen_instance.add(instance_id)
        desired_state = item.get("desired_state")
        if desired_state not in ALLOWED_DESIRED_STATES:
            raise BusinessError(
                "INVALID_DESIRED_STATE",
                f"Request {seq} has invalid desired_state",
                request_seq=seq,
                template_name=template_name,
                desired_state=desired_state,
            )
        clock = item.get("logical_clock")
        require(
            is_nonnegative_int(clock),
            "INVALID_LOGICAL_CLOCK",
            f"Request {seq} logical_clock must be non-negative",
            request_seq=seq,
            template_name=template_name,
            logical_clock=clock,
        )
        if previous_clock >= 0 and clock + clock_tolerance_ms < previous_clock:
            raise BusinessError(
                "CLOCK_ROLLBACK",
                "Request stream logical clock rolled back beyond tolerance",
                request_seq=seq,
                template_name=template_name,
                logical_clock=clock,
                previous_logical_clock=previous_clock,
                tolerance_ms=clock_tolerance_ms,
            )
        previous_clock = max(previous_clock, clock)
        round_no = item.get("round")
        require(
            is_positive_int(round_no),
            "INVALID_ROUND",
            f"Request {seq} round must be a positive integer",
            request_seq=seq,
            template_name=template_name,
            round=round_no,
        )
        logical_cost = item.get("logical_cost_ms")
        require(
            is_nonnegative_int(logical_cost),
            "INVALID_LOGICAL_COST",
            f"Request {seq} logical_cost_ms must be non-negative",
            request_seq=seq,
            template_name=template_name,
            logical_cost_ms=logical_cost,
        )
        inject_failure = item.get("inject_start_failure", False)
        require(
            isinstance(inject_failure, bool),
            "INVALID_REQUEST_STREAM",
            f"Request {seq} inject_start_failure must be boolean",
            request_seq=seq,
            template_name=template_name,
        )
        requests.append(
            InstantiationRequest(
                request_seq=seq,
                template_name=template_name,
                target_topology_namespace=namespace,
                instance_id=instance_id,
                desired_state=desired_state,
                logical_clock=clock,
                round_no=round_no,
                logical_cost_ms=logical_cost,
                inject_start_failure=inject_failure,
            )
        )
    return sorted(requests, key=lambda request: request.request_seq)


def validate_topology(payload: Any) -> Topology:
    data = _as_dict(
        payload,
        "INVALID_TOPOLOGY",
        "Topology configuration must be a JSON object",
    )
    require(
        data.get("format") == "topology-capacity/v1",
        "INVALID_TOPOLOGY",
        "Unsupported topology configuration format",
        actual_format=data.get("format"),
    )
    max_depth = data.get("max_depth")
    require(
        is_positive_int(max_depth),
        "INVALID_TOPOLOGY",
        "Topology max_depth must be a positive integer",
        max_depth=max_depth,
    )
    nodes = _as_dict(
        data.get("nodes"),
        "INVALID_TOPOLOGY",
        "Topology nodes must be an object",
    )
    checked_nodes: dict[str, dict[str, Any]] = {}
    for node_id, raw_node in nodes.items():
        if not isinstance(node_id, str) or not INSTANCE_RE.fullmatch(node_id):
            raise BusinessError(
                "INVALID_TOPOLOGY",
                f"Invalid node id: {node_id!r}",
                node_id=node_id,
            )
        node = _as_dict(
            raw_node,
            "INVALID_TOPOLOGY",
            f"Node {node_id} must be an object",
            node_id=node_id,
        )
        resources = _as_dict(
            node.get("resources"),
            "INVALID_TOPOLOGY",
            f"Node {node_id} resources must be an object",
            node_id=node_id,
        )
        checked_resources: dict[str, int] = {}
        for resource, capacity in resources.items():
            if not isinstance(resource, str) or not RESOURCE_RE.fullmatch(resource):
                raise BusinessError(
                    "INVALID_TOPOLOGY",
                    f"Node {node_id} has invalid resource {resource!r}",
                    node_id=node_id,
                    resource_name=resource,
                )
            require(
                is_nonnegative_int(capacity),
                "INVALID_TOPOLOGY",
                f"Node {node_id} resource {resource} capacity must be non-negative",
                node_id=node_id,
                resource_name=resource,
                capacity=capacity,
            )
            checked_resources[resource] = capacity
        concurrency = node.get("template_concurrency", {})
        concurrency = _as_dict(
            concurrency,
            "INVALID_TOPOLOGY",
            f"Node {node_id} template_concurrency must be an object",
            node_id=node_id,
        )
        checked_concurrency: dict[str, int] = {}
        for template_name, limit in concurrency.items():
            if not isinstance(template_name, str) or not TEMPLATE_RE.fullmatch(
                template_name
            ):
                raise BusinessError(
                    "INVALID_TOPOLOGY",
                    f"Node {node_id} has invalid concurrency template {template_name!r}",
                    node_id=node_id,
                    template_name=template_name,
                )
            require(
                is_positive_int(limit),
                "INVALID_TOPOLOGY",
                f"Node {node_id} template {template_name} concurrency must be positive",
                node_id=node_id,
                template_name=template_name,
                limit=limit,
            )
            checked_concurrency[template_name] = limit
        checked_nodes[node_id] = {
            "resources": checked_resources,
            "template_concurrency": checked_concurrency,
        }
    namespaces_raw = _as_dict(
        data.get("namespaces"),
        "INVALID_TOPOLOGY",
        "Topology namespaces must be an object",
    )
    checked_namespaces: dict[str, NamespaceTopology] = {}
    for namespace, raw_namespace in namespaces_raw.items():
        if not isinstance(namespace, str) or not INSTANCE_RE.fullmatch(namespace):
            raise BusinessError(
                "INVALID_TOPOLOGY",
                f"Invalid topology namespace: {namespace!r}",
                namespace=namespace,
            )
        item = _as_dict(
            raw_namespace,
            "INVALID_TOPOLOGY",
            f"Namespace {namespace} must be an object",
            namespace=namespace,
        )
        node_id = item.get("node_id")
        if node_id not in checked_nodes:
            raise BusinessError(
                "INVALID_TOPOLOGY",
                f"Namespace {namespace} references unknown node {node_id!r}",
                namespace=namespace,
                node_id=node_id,
            )
        constraints = item.get("topology_constraints", {})
        constraints = _as_dict(
            constraints,
            "INVALID_TOPOLOGY",
            f"Namespace {namespace} topology_constraints must be an object",
            namespace=namespace,
        )
        namespace_depth = constraints.get("max_depth", max_depth)
        require(
            is_positive_int(namespace_depth),
            "INVALID_TOPOLOGY",
            f"Namespace {namespace} max_depth must be positive",
            namespace=namespace,
            max_depth=namespace_depth,
        )
        checked_namespaces[namespace] = NamespaceTopology(
            name=namespace,
            node_id=node_id,
            max_depth=namespace_depth,
        )
    return Topology(nodes=checked_nodes, namespaces=checked_namespaces)


def load_validated_samples(
    workdir: Path,
    *,
    clock_tolerance_ms: int = CLOCK_TOLERANCE_MS,
) -> tuple[
    str,
    int,
    dict[str, ServiceTemplate],
    list[InstantiationRequest],
    Topology,
]:
    samples = workdir / "samples"
    manifest_version, injected_clock, templates = validate_template_manifest(
        read_json(samples / "templates.json")
    )
    requests = validate_requests(
        read_json(samples / "requests.json"),
        templates=templates,
        clock_tolerance_ms=clock_tolerance_ms,
    )
    topology = validate_topology(read_json(samples / "topology.json"))
    return manifest_version, injected_clock, templates, requests, topology


def check_manifest_version_monotonic(
    previous: str | None,
    current: str,
    **diagnostics: Any,
) -> None:
    if previous is None:
        return
    from .util import compare_semver

    if compare_semver(current, previous) < 0:
        raise BusinessError(
            "MANIFEST_VERSION_ROLLBACK",
            f"Template manifest version rolled back from {previous} to {current}",
            version=current,
            previous_version=previous,
            **diagnostics,
        )


def dependency_graph_from_templates(
    templates: Iterable[ServiceTemplate],
) -> dict[str, tuple[str, ...]]:
    return {template.name: template.dependencies for template in templates}
