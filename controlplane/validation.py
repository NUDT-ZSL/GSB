"""Sample loading and deterministic business validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .canonical import sha256_hex
from .errors import ControlPlaneError
from .numbers import MAX_SAFE_INT64, as_decimal, convert_values

PARAMETERS: tuple[str, ...] = (
    "timeout_ms",
    "retry_multiplier",
    "sampling_rate",
    "replica_weight",
    "capacity_limit",
)
INTEGER_PARAMETERS = frozenset({"timeout_ms", "capacity_limit"})
METRICS: tuple[str, ...] = (
    "worst_link_timeout_ms",
    "effective_capacity",
    "weighted_sampling_rate",
)
SOURCE_FILES: tuple[str, ...] = (
    "clusters.json",
    "regions.json",
    "config_templates.json",
    "distribution_policy.json",
    "logical_clock.json",
)


@dataclass(frozen=True)
class RegionSpec:
    name: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class ClusterSpec:
    name: str
    region: str
    priority: int
    allow_differential_override: bool
    capacity_source_unit: str
    capacity_target_unit: str
    capacity_factor: int
    dispatch_latency_ms: int
    dispatch_fail_attempts: int


@dataclass(frozen=True)
class Templates:
    global_params: Mapping[str, Any]
    region_params: Mapping[str, Mapping[str, Any]]
    cluster_params: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Policy:
    fixed_point_scales: Mapping[str, int]
    floating_tolerance: Mapping[str, Any]
    error_budget: Mapping[str, Any]
    max_parallel_per_round: int
    retry_budget: int
    logical_clock_step: int
    logical_time_budget_ms: int


@dataclass(frozen=True)
class LogicalClock:
    start: int
    steps: tuple[int, ...]

    def delta_for_round(self, round_no: int, fallback: int) -> int:
        if 1 <= round_no <= len(self.steps):
            return self.steps[round_no - 1]
        return fallback


@dataclass(frozen=True)
class Sample:
    regions: tuple[RegionSpec, ...]
    clusters: tuple[ClusterSpec, ...]
    templates: Templates
    policy: Policy
    logical_clock: LogicalClock
    region_waves: tuple[tuple[str, ...], ...]
    region_ancestors: Mapping[str, tuple[str, ...]]
    snapshot: Mapping[str, Any]
    source_hash: str
    version_id: str

    def cluster_by_name(self) -> dict[str, ClusterSpec]:
        return {cluster.name: cluster for cluster in self.clusters}

    def region_by_name(self) -> dict[str, RegionSpec]:
        return {region.name: region for region in self.regions}


class _DuplicateKeyError(ValueError):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ControlPlaneError(
            "E_SAMPLE_IO",
            f"cannot read sample file: {exc}",
            path=str(path),
        ) from exc
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            object_pairs_hook=_object_no_duplicates,
        )
    except _DuplicateKeyError as exc:
        raise ControlPlaneError(
            "E_DUPLICATE_KEY",
            f"duplicate JSON object key: {exc.key}",
            path=str(path),
        ) from exc
    except json.JSONDecodeError as exc:
        raise ControlPlaneError(
            "E_PARSE_JSON",
            f"invalid JSON: {exc.msg}",
            path=f"{path}:{exc.lineno}:{exc.colno}",
        ) from exc


def _require_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ControlPlaneError("E_INVALID_TYPE", "expected a JSON object", path=path)
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ControlPlaneError("E_INVALID_TYPE", "expected a JSON array", path=path)
    return value


def _require_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ControlPlaneError(
            "E_INVALID_TYPE",
            "expected a non-empty string" if nonempty else "expected a string",
            path=path,
        )
    return value


def _require_int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is int:
        normalized = value
    elif isinstance(value, Decimal) and value.is_finite():
        integral = value.to_integral_value()
        if value != integral:
            raise ControlPlaneError(
                "E_INVALID_TYPE", "expected an integer", path=path
            )
        normalized = int(integral)
    else:
        raise ControlPlaneError("E_INVALID_TYPE", "expected an integer", path=path)
    if minimum is not None and normalized < minimum:
        raise ControlPlaneError(
            "E_INVALID_RANGE", f"integer must be >= {minimum}", path=path
        )
    if maximum is not None and normalized > maximum:
        raise ControlPlaneError(
            "E_INVALID_RANGE", f"integer must be <= {maximum}", path=path
        )
    return normalized


def _require_bool(value: Any, path: str) -> bool:
    if type(value) is not bool:
        raise ControlPlaneError("E_INVALID_TYPE", "expected a boolean", path=path)
    return value


def _require_number(
    value: Any,
    path: str,
    *,
    minimum: Any | None = None,
    maximum: Any | None = None,
    minimum_inclusive: bool = True,
) -> Decimal:
    if type(value) is bool or not isinstance(value, (int, float, Decimal)):
        raise ControlPlaneError("E_INVALID_TYPE", "expected a number", path=path)
    try:
        dec = as_decimal(value)
    except ControlPlaneError as exc:
        raise ControlPlaneError(exc.code, exc.message, path=path) from exc
    if not dec.is_finite():
        raise ControlPlaneError("E_INVALID_NUMBER", "number must be finite", path=path)
    if minimum is not None:
        lower = as_decimal(minimum)
        if dec < lower or (not minimum_inclusive and dec == lower):
            operator = ">=" if minimum_inclusive else ">"
            raise ControlPlaneError(
                "E_INVALID_RANGE", f"number must be {operator} {lower}", path=path
            )
    if maximum is not None and dec > as_decimal(maximum):
        raise ControlPlaneError(
            "E_INVALID_RANGE", f"number must be <= {as_decimal(maximum)}", path=path
        )
    return dec


def _check_unknown_keys(
    mapping: Mapping[str, Any], allowed: set[str], path: str
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ControlPlaneError(
            "E_UNKNOWN_FIELD", f"unknown field(s): {', '.join(unknown)}", path=path
        )


def _validate_params(
    raw: Any,
    path: str,
    *,
    require_all: bool = False,
) -> dict[str, Any]:
    params = dict(_require_object(raw, path))
    _check_unknown_keys(params, set(PARAMETERS), path)
    if require_all:
        missing = [name for name in PARAMETERS if name not in params]
        if missing:
            raise ControlPlaneError(
                "E_MISSING_PARAM",
                f"missing required parameter(s): {', '.join(missing)}",
                path=path,
            )

    for name in PARAMETERS:
        if name not in params:
            continue
        value = params[name]
        item_path = f"{path}.{name}"
        if name in INTEGER_PARAMETERS:
            params[name] = _require_int(value, item_path, minimum=1)
            continue
        if name == "retry_multiplier":
            params[name] = _require_number(
                value, item_path, minimum=0, minimum_inclusive=False
            )
        elif name == "sampling_rate":
            params[name] = _require_number(value, item_path, minimum=0, maximum=1)
        elif name == "replica_weight":
            params[name] = _require_number(
                value, item_path, minimum=0, minimum_inclusive=False
            )
    return params


def _parse_clusters(raw: Any) -> tuple[ClusterSpec, ...]:
    root = _require_object(raw, "clusters")
    _check_unknown_keys(root, {"schema_version", "clusters"}, "clusters")
    if root.get("schema_version") != 1:
        raise ControlPlaneError(
            "E_SCHEMA_VERSION",
            "clusters schema_version must be 1",
            path="clusters.schema_version",
        )
    items = _require_list(root.get("clusters"), "clusters.clusters")
    if not items:
        raise ControlPlaneError(
            "E_EMPTY", "cluster list must not be empty", path="clusters.clusters"
        )
    seen: set[str] = set()
    result: list[ClusterSpec] = []
    for index, item in enumerate(items):
        path = f"clusters.clusters[{index}]"
        obj = _require_object(item, path)
        _check_unknown_keys(
            obj,
            {
                "name",
                "region",
                "priority",
                "capacity_unit",
                "allow_differential_override",
                "dispatch",
            },
            path,
        )
        name = _require_string(obj.get("name"), f"{path}.name")
        if name in seen:
            raise ControlPlaneError(
                "E_DUPLICATE_CLUSTER",
                f"duplicate cluster name: {name}",
                path=f"{path}.name",
            )
        seen.add(name)
        region = _require_string(obj.get("region"), f"{path}.region")
        priority = _require_int(obj.get("priority"), f"{path}.priority", minimum=0)
        allow = _require_bool(
            obj.get("allow_differential_override"),
            f"{path}.allow_differential_override",
        )

        unit_path = f"{path}.capacity_unit"
        unit = _require_object(obj.get("capacity_unit"), unit_path)
        _check_unknown_keys(unit, {"source", "target", "factor"}, unit_path)
        source_unit = _require_string(unit.get("source"), f"{unit_path}.source")
        target_unit = _require_string(unit.get("target"), f"{unit_path}.target")
        factor = _require_int(unit.get("factor"), f"{unit_path}.factor", minimum=1)

        dispatch_path = f"{path}.dispatch"
        dispatch = _require_object(obj.get("dispatch", {}), dispatch_path)
        _check_unknown_keys(
            dispatch, {"latency_ms", "fail_attempts"}, dispatch_path
        )
        latency = _require_int(
            dispatch.get("latency_ms", 1),
            f"{dispatch_path}.latency_ms",
            minimum=1,
        )
        fail_attempts = _require_int(
            dispatch.get("fail_attempts", 0),
            f"{dispatch_path}.fail_attempts",
            minimum=0,
        )
        result.append(
            ClusterSpec(
                name=name,
                region=region,
                priority=priority,
                allow_differential_override=allow,
                capacity_source_unit=source_unit,
                capacity_target_unit=target_unit,
                capacity_factor=factor,
                dispatch_latency_ms=latency,
                dispatch_fail_attempts=fail_attempts,
            )
        )
    return tuple(result)


def _parse_regions(raw: Any) -> tuple[RegionSpec, ...]:
    root = _require_object(raw, "regions")
    _check_unknown_keys(root, {"schema_version", "regions"}, "regions")
    if root.get("schema_version") != 1:
        raise ControlPlaneError(
            "E_SCHEMA_VERSION",
            "regions schema_version must be 1",
            path="regions.schema_version",
        )
    items = _require_list(root.get("regions"), "regions.regions")
    if not items:
        raise ControlPlaneError(
            "E_EMPTY", "region list must not be empty", path="regions.regions"
        )
    seen: set[str] = set()
    result: list[RegionSpec] = []
    for index, item in enumerate(items):
        path = f"regions.regions[{index}]"
        obj = _require_object(item, path)
        _check_unknown_keys(obj, {"name", "depends_on"}, path)
        name = _require_string(obj.get("name"), f"{path}.name")
        if name in seen:
            raise ControlPlaneError(
                "E_DUPLICATE_REGION",
                f"duplicate region name: {name}",
                path=f"{path}.name",
            )
        seen.add(name)
        dependency_items = _require_list(obj.get("depends_on"), f"{path}.depends_on")
        dependencies: list[str] = []
        for dep_index, dependency in enumerate(dependency_items):
            dep_name = _require_string(
                dependency, f"{path}.depends_on[{dep_index}]"
            )
            if dep_name in dependencies:
                raise ControlPlaneError(
                    "E_DUPLICATE_DEPENDENCY",
                    f"duplicate region dependency: {dep_name}",
                    path=f"{path}.depends_on[{dep_index}]",
                )
            dependencies.append(dep_name)
        result.append(RegionSpec(name=name, depends_on=tuple(dependencies)))
    return tuple(result)


def _validate_region_graph(
    regions: tuple[RegionSpec, ...],
) -> tuple[tuple[tuple[str, ...], ...], dict[str, tuple[str, ...]]]:
    by_name = {region.name: region for region in regions}
    for region in regions:
        for dependency in region.depends_on:
            if dependency not in by_name:
                raise ControlPlaneError(
                    "E_UNKNOWN_REGION",
                    f"region {region.name} depends on unknown region {dependency}",
                    path=f"regions.{region.name}.depends_on",
                )
            if dependency == region.name:
                raise ControlPlaneError(
                    "E_REGION_CYCLE",
                    f"region {region.name} depends on itself",
                    path=f"regions.{region.name}.depends_on",
                )

    indegree = {region.name: len(region.depends_on) for region in regions}
    children: dict[str, list[str]] = {region.name: [] for region in regions}
    for region in regions:
        for dependency in region.depends_on:
            children[dependency].append(region.name)
    for child_list in children.values():
        child_list.sort()

    ready = sorted(name for name, degree in indegree.items() if degree == 0)
    processed: list[str] = []
    waves: list[tuple[str, ...]] = []
    while ready:
        wave = tuple(sorted(ready))
        waves.append(wave)
        processed.extend(wave)
        next_ready: list[str] = []
        for name in wave:
            for child in children[name]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_ready.append(child)
        ready = sorted(next_ready)
    if len(processed) != len(regions):
        cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
        raise ControlPlaneError(
            "E_REGION_CYCLE",
            f"region dependency cycle involves: {', '.join(cyclic)}",
            path="regions.regions",
        )

    ancestors: dict[str, tuple[str, ...]] = {}
    for name in processed:
        nodes: set[str] = set()
        for dependency in by_name[name].depends_on:
            nodes.add(dependency)
            nodes.update(ancestors[dependency])
        ancestors[name] = tuple(sorted(nodes))
    return tuple(waves), ancestors


def _parse_templates(
    raw: Any,
    region_names: set[str],
    cluster_names: set[str],
) -> Templates:
    root = _require_object(raw, "config_templates")
    _check_unknown_keys(
        root, {"schema_version", "global", "regions", "clusters"}, "config_templates"
    )
    if root.get("schema_version") != 1:
        raise ControlPlaneError(
            "E_SCHEMA_VERSION",
            "config_templates schema_version must be 1",
            path="config_templates.schema_version",
        )

    global_obj = _require_object(root.get("global"), "config_templates.global")
    _check_unknown_keys(global_obj, {"scope", "params"}, "config_templates.global")
    global_scope = _require_string(
        global_obj.get("scope"), "config_templates.global.scope"
    )
    if global_scope != "global":
        raise ControlPlaneError(
            "E_INVALID_SCOPE",
            "global layer scope must be 'global'",
            path="config_templates.global.scope",
        )
    global_params = _validate_params(
        global_obj.get("params"), "config_templates.global.params", require_all=True
    )

    region_params: dict[str, dict[str, Any]] = {}
    region_items = _require_list(
        root.get("regions"), "config_templates.regions"
    )
    for index, item in enumerate(region_items):
        path = f"config_templates.regions[{index}]"
        obj = _require_object(item, path)
        _check_unknown_keys(obj, {"scope", "params"}, path)
        scope = _require_string(obj.get("scope"), f"{path}.scope")
        if scope in region_params:
            raise ControlPlaneError(
                "E_DUPLICATE_SCOPE",
                f"duplicate region layer scope: {scope}",
                path=f"{path}.scope",
            )
        if scope not in region_names:
            raise ControlPlaneError(
                "E_UNKNOWN_REGION",
                f"region layer references unknown region: {scope}",
                path=f"{path}.scope",
            )
        region_params[scope] = _validate_params(obj.get("params"), f"{path}.params")

    cluster_params: dict[str, dict[str, Any]] = {}
    cluster_items = _require_list(
        root.get("clusters"), "config_templates.clusters"
    )
    for index, item in enumerate(cluster_items):
        path = f"config_templates.clusters[{index}]"
        obj = _require_object(item, path)
        _check_unknown_keys(obj, {"scope", "params"}, path)
        scope = _require_string(obj.get("scope"), f"{path}.scope")
        if scope in cluster_params:
            raise ControlPlaneError(
                "E_DUPLICATE_SCOPE",
                f"duplicate cluster layer scope: {scope}",
                path=f"{path}.scope",
            )
        if scope not in cluster_names:
            raise ControlPlaneError(
                "E_UNKNOWN_CLUSTER",
                f"cluster layer references unknown cluster: {scope}",
                path=f"{path}.scope",
            )
        cluster_params[scope] = _validate_params(obj.get("params"), f"{path}.params")

    return Templates(
        global_params=global_params,
        region_params=region_params,
        cluster_params=cluster_params,
    )


def _parse_policy(raw: Any) -> Policy:
    root = _require_object(raw, "distribution_policy")
    allowed = {
        "schema_version",
        "policy",
        "fixed_point_scales",
        "floating_tolerance",
        "error_budget",
        "max_parallel_per_round",
        "retry_budget",
        "logical_clock_step",
        "logical_time_budget_ms",
    }
    _check_unknown_keys(root, allowed, "distribution_policy")
    if root.get("schema_version") != 1:
        raise ControlPlaneError(
            "E_SCHEMA_VERSION",
            "distribution_policy schema_version must be 1",
            path="distribution_policy.schema_version",
        )
    if "policy" in root:
        policy_obj = root["policy"]
    else:
        policy_obj = {key: value for key, value in root.items() if key != "schema_version"}
    if not isinstance(policy_obj, dict):
        raise ControlPlaneError(
            "E_INVALID_TYPE",
            "policy must be a JSON object",
            path="distribution_policy.policy",
        )
    policy = _require_object(policy_obj, "distribution_policy.policy")
    required_policy_keys = {
        "fixed_point_scales",
        "floating_tolerance",
        "error_budget",
        "max_parallel_per_round",
        "retry_budget",
        "logical_clock_step",
        "logical_time_budget_ms",
    }
    _check_unknown_keys(policy, required_policy_keys, "distribution_policy.policy")
    missing = sorted(required_policy_keys - set(policy))
    if missing:
        raise ControlPlaneError(
            "E_MISSING_POLICY",
            f"missing policy field(s): {', '.join(missing)}",
            path="distribution_policy.policy",
        )

    scales_obj = _require_object(
        policy["fixed_point_scales"],
        "distribution_policy.policy.fixed_point_scales",
    )
    _check_unknown_keys(
        scales_obj,
        set(PARAMETERS),
        "distribution_policy.policy.fixed_point_scales",
    )
    scales: dict[str, int] = {}
    for name in PARAMETERS:
        if name not in scales_obj:
            raise ControlPlaneError(
                "E_MISSING_POLICY",
                f"missing fixed-point scale for {name}",
                path="distribution_policy.policy.fixed_point_scales",
            )
        scales[name] = _require_int(
            scales_obj[name],
            f"distribution_policy.policy.fixed_point_scales.{name}",
            minimum=1,
        )

    tolerance_obj = _require_object(
        policy["floating_tolerance"],
        "distribution_policy.policy.floating_tolerance",
    )
    tolerance_keys = set(PARAMETERS) | set(METRICS)
    _check_unknown_keys(
        tolerance_obj,
        tolerance_keys,
        "distribution_policy.policy.floating_tolerance",
    )
    tolerances: dict[str, Decimal] = {}
    for name in PARAMETERS + METRICS:
        if name not in tolerance_obj:
            raise ControlPlaneError(
                "E_MISSING_POLICY",
                f"missing floating tolerance for {name}",
                path="distribution_policy.policy.floating_tolerance",
            )
        tolerances[name] = _require_number(
            tolerance_obj[name],
            f"distribution_policy.policy.floating_tolerance.{name}",
            minimum=0,
            minimum_inclusive=False,
        )

    budget_obj = _require_object(
        policy["error_budget"], "distribution_policy.policy.error_budget"
    )
    _check_unknown_keys(
        budget_obj, set(METRICS), "distribution_policy.policy.error_budget"
    )
    budgets: dict[str, Decimal] = {}
    for name in METRICS:
        if name not in budget_obj:
            raise ControlPlaneError(
                "E_MISSING_POLICY",
                f"missing error budget for {name}",
                path="distribution_policy.policy.error_budget",
            )
        budgets[name] = _require_number(
            budget_obj[name],
            f"distribution_policy.policy.error_budget.{name}",
            minimum=0,
            minimum_inclusive=False,
        )

    return Policy(
        fixed_point_scales=scales,
        floating_tolerance=tolerances,
        error_budget=budgets,
        max_parallel_per_round=_require_int(
            policy["max_parallel_per_round"],
            "distribution_policy.policy.max_parallel_per_round",
            minimum=1,
        ),
        retry_budget=_require_int(
            policy["retry_budget"],
            "distribution_policy.policy.retry_budget",
            minimum=0,
        ),
        logical_clock_step=_require_int(
            policy["logical_clock_step"],
            "distribution_policy.policy.logical_clock_step",
            minimum=1,
        ),
        logical_time_budget_ms=_require_int(
            policy["logical_time_budget_ms"],
            "distribution_policy.policy.logical_time_budget_ms",
            minimum=1,
        ),
    )


def _parse_logical_clock(raw: Any) -> LogicalClock:
    root = _require_object(raw, "logical_clock")
    _check_unknown_keys(root, {"schema_version", "logical_clock"}, "logical_clock")
    if root.get("schema_version") != 1:
        raise ControlPlaneError(
            "E_SCHEMA_VERSION",
            "logical_clock schema_version must be 1",
            path="logical_clock.schema_version",
        )
    obj = _require_object(root.get("logical_clock"), "logical_clock.logical_clock")
    _check_unknown_keys(obj, {"start", "steps"}, "logical_clock.logical_clock")
    start = _require_int(
        obj.get("start"), "logical_clock.logical_clock.start", minimum=0
    )
    raw_steps = _require_list(
        obj.get("steps"), "logical_clock.logical_clock.steps"
    )
    steps = tuple(
        _require_int(
            value, f"logical_clock.logical_clock.steps[{index}]", minimum=1
        )
        for index, value in enumerate(raw_steps)
    )
    return LogicalClock(start=start, steps=steps)


def _validate_fixed_ranges(
    templates: Templates, policy: Policy, clusters: tuple[ClusterSpec, ...]
) -> None:
    fixed_by_parameter: dict[str, list[int]] = {}
    for name in PARAMETERS:
        scale = policy.fixed_point_scales[name]
        values: list[Any] = [templates.global_params[name]]
        for scope in sorted(templates.region_params):
            if name in templates.region_params[scope]:
                values.append(templates.region_params[scope][name])
        for scope in sorted(templates.cluster_params):
            if name in templates.cluster_params[scope]:
                values.append(templates.cluster_params[scope][name])
        try:
            batch = convert_values(values, scale)
            fixed_by_parameter[name] = [int(value) for value in batch.fixed]
            convert_values([policy.floating_tolerance[name]], scale)
        except ControlPlaneError as exc:
            raise ControlPlaneError(
                exc.code,
                f"parameter {name}: {exc.message}",
                path="distribution_policy.policy.fixed_point_scales",
            ) from exc
    max_timeout = max(fixed_by_parameter["timeout_ms"])
    if max_timeout * (len({cluster.region for cluster in clusters}) + 1) > MAX_SAFE_INT64:
        raise ControlPlaneError(
            "E_FIXED_RANGE",
            "worst link timeout exceeds signed 64-bit range",
            path="config_templates",
        )
    max_capacity = max(fixed_by_parameter["capacity_limit"])
    for cluster in clusters:
        if max_capacity * cluster.capacity_factor > MAX_SAFE_INT64:
            raise ControlPlaneError(
                "E_FIXED_RANGE",
                f"effective capacity for {cluster.name} exceeds signed 64-bit range",
                path=f"clusters.{cluster.name}.capacity_unit.factor",
            )
    sampling_scale = policy.fixed_point_scales["sampling_rate"]
    weight_scale = policy.fixed_point_scales["replica_weight"]
    weighted_scale = sampling_scale * weight_scale
    if weighted_scale > MAX_SAFE_INT64:
        raise ControlPlaneError(
            "E_FIXED_RANGE",
            "weighted sampling result scale exceeds signed 64-bit range",
            path="distribution_policy.policy.fixed_point_scales",
        )
    max_sampling = max(fixed_by_parameter["sampling_rate"])
    max_weight = max(fixed_by_parameter["replica_weight"])
    if max_sampling * max_weight > MAX_SAFE_INT64:
        raise ControlPlaneError(
            "E_FIXED_RANGE",
            "weighted sampling result exceeds signed 64-bit range",
            path="config_templates",
        )
    tolerance_scales = {
        "worst_link_timeout_ms": policy.fixed_point_scales["timeout_ms"],
        "effective_capacity": policy.fixed_point_scales["capacity_limit"],
        "weighted_sampling_rate": weighted_scale,
    }
    try:
        for metric, scale in tolerance_scales.items():
            convert_values([policy.floating_tolerance[metric]], scale)
    except ControlPlaneError as exc:
        raise ControlPlaneError(
            exc.code,
            f"derived metric tolerance: {exc.message}",
            path="distribution_policy.policy.floating_tolerance",
        ) from exc


def load_sample(sample_dir: str | Path) -> Sample:
    """Load and fully validate a sample directory before any persistence."""

    directory = Path(sample_dir)
    if not directory.is_dir():
        raise ControlPlaneError(
            "E_SAMPLE_DIR", f"sample directory does not exist: {directory}"
        )
    parsed: dict[str, Any] = {}
    for filename in SOURCE_FILES:
        parsed[filename] = _read_json(directory / filename)

    clusters = _parse_clusters(parsed["clusters.json"])
    regions = _parse_regions(parsed["regions.json"])
    region_waves, region_ancestors = _validate_region_graph(regions)
    region_names = {region.name for region in regions}
    cluster_names = {cluster.name for cluster in clusters}

    for cluster in clusters:
        if cluster.region not in region_names:
            raise ControlPlaneError(
                "E_UNKNOWN_REGION",
                f"cluster {cluster.name} references unknown region {cluster.region}",
                path=f"clusters.{cluster.name}.region",
            )
    templates = _parse_templates(
        parsed["config_templates.json"], region_names, cluster_names
    )
    region_layer_scopes = set(templates.region_params)
    cluster_layer_scopes = set(templates.cluster_params)
    all_scopes = {"global"} | region_layer_scopes | cluster_layer_scopes
    expected_scope_count = (
        1 + len(region_layer_scopes) + len(cluster_layer_scopes)
    )
    if len(all_scopes) != expected_scope_count:
        raise ControlPlaneError(
            "E_DUPLICATE_SCOPE",
            "layer scopes must be unique across global, region, and cluster layers",
            path="config_templates",
        )
    policy = _parse_policy(parsed["distribution_policy.json"])
    logical_clock = _parse_logical_clock(parsed["logical_clock.json"])
    _validate_fixed_ranges(templates, policy, clusters)

    snapshot = {
        "clusters": parsed["clusters.json"],
        "regions": parsed["regions.json"],
        "config_templates": parsed["config_templates.json"],
        "distribution_policy": parsed["distribution_policy.json"],
        "logical_clock": parsed["logical_clock.json"],
    }
    source_hash = sha256_hex(snapshot)
    return Sample(
        regions=regions,
        clusters=clusters,
        templates=templates,
        policy=policy,
        logical_clock=logical_clock,
        region_waves=region_waves,
        region_ancestors=region_ancestors,
        snapshot=snapshot,
        source_hash=source_hash,
        version_id="v" + source_hash[:16],
    )
