from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import fail
from .util import (
    content_digest,
    is_finite_number,
    is_int,
)


VALID_MODES = {"active_passive", "dual_active"}


@dataclass(frozen=True, slots=True)
class Instance:
    instance_id: str
    health_weight: float
    replica_seed: int
    data_variant: int


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    domain: str
    upstream: tuple[str, ...]
    health_weight: float
    data_seed: int
    row_count: int
    initial_primary: str
    instances: tuple[Instance, ...]

    def instance_map(self) -> dict[str, Instance]:
        return {instance.instance_id: instance for instance in self.instances}

    def ordered_instances(self) -> list[Instance]:
        return sorted(
            self.instances,
            key=lambda item: (-item.health_weight, item.instance_id),
        )


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    mode: str
    trigger_metric: str
    trigger_threshold: int
    cooldown_ms: int
    failback_ladder: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Policy:
    version: int
    freeze_order: tuple[str, ...]
    freeze_batch_size: int
    max_concurrency: int
    time_budget_ms: int
    clock_regression_tolerance_ms: int
    freeze_probe_cost_ms: int
    switch_probe_cost_ms: int
    services: dict[str, ServicePolicy]


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    instance_id: str
    metric: str
    value: int | float
    window: str
    logical_time_ms: int


@dataclass(frozen=True, slots=True)
class Sample:
    sample_id: str
    raw: dict[str, Any]
    topology_raw: dict[str, Any]
    policy_raw: dict[str, Any]
    events_raw: list[dict[str, Any]]
    domains: tuple[str, ...]
    services: dict[str, Service]
    policy: Policy
    events: tuple[Event, ...]
    sample_digest: str
    policy_digest: str

    def instance_service(self) -> dict[str, str]:
        return {
            instance.instance_id: service.name
            for service in self.services.values()
            for instance in service.instances
        }

    def event_map(self) -> dict[int, Event]:
        return {event.seq: event for event in self.events}


def _require_object(value: Any, path: str, code: str = "INVALID_SAMPLE") -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(code, "Expected an object.", field_path=path)
    return value


def _require_list(value: Any, path: str, code: str = "INVALID_SAMPLE") -> list[Any]:
    if not isinstance(value, list):
        fail(code, "Expected an array.", field_path=path)
    return value


def _require_string(value: Any, path: str, code: str = "INVALID_SAMPLE") -> str:
    if not isinstance(value, str) or not value.strip():
        fail(code, "Expected a non-empty string.", field_path=path)
    return value.strip()


def _require_nonnegative_int(
    value: Any,
    path: str,
    code: str = "INVALID_POLICY",
) -> int:
    if not is_int(value) or value < 0:
        fail(code, "Expected a non-negative integer.", field_path=path)
    return value


def _require_positive_int(
    value: Any,
    path: str,
    code: str = "INVALID_POLICY",
) -> int:
    if not is_int(value) or value <= 0:
        fail(code, "Expected a positive integer.", field_path=path)
    return value


def _parse_ladder(
    value: Any,
    path: str,
    service: str,
    domain: str,
) -> tuple[float, ...]:
    context = {"domain": domain, "service": service}
    if not isinstance(value, list) or not value:
        fail(
            "INVALID_FAILBACK_LADDER",
            "Failback ladder must be a non-empty array.",
            field_path=path,
            context=context,
        )
    decimal_values: list[Decimal] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not is_finite_number(item):
            fail(
                "INVALID_FAILBACK_LADDER",
                "Failback ratio must be a finite number.",
                field_path=item_path,
                context=context,
            )
        try:
            ratio = Decimal(str(item))
        except InvalidOperation:
            fail(
                "INVALID_FAILBACK_LADDER",
                "Failback ratio is not a decimal number.",
                field_path=item_path,
                context=context,
            )
        if ratio < 0 or ratio > 1:
            fail(
                "INVALID_FAILBACK_LADDER",
                "Failback ratio must be between zero and one.",
                field_path=item_path,
                context=context,
            )
        if decimal_values and ratio >= decimal_values[-1]:
            fail(
                "INVALID_FAILBACK_LADDER",
                "Failback ratios must be strictly decreasing.",
                field_path=item_path,
                context=context,
            )
        decimal_values.append(ratio)
    if decimal_values[-1] != 0:
        fail(
            "INVALID_FAILBACK_LADDER",
            "Failback ladder must end with zero.",
            field_path=f"{path}[{len(decimal_values) - 1}]",
            context=context,
        )
    return tuple(float(item) for item in decimal_values)


def _validate_topology(
    raw: dict[str, Any],
) -> tuple[tuple[str, ...], dict[str, Service]]:
    domains_raw = _require_list(
        raw.get("isolation_domains"),
        "topology.isolation_domains",
        "INVALID_TOPOLOGY",
    )
    domains: list[str] = []
    for index, item in enumerate(domains_raw):
        domain = _require_string(
            item,
            f"topology.isolation_domains[{index}]",
            "INVALID_TOPOLOGY",
        )
        if domain in domains:
            fail(
                "INVALID_TOPOLOGY",
                "Isolation domain names must be unique.",
                field_path=f"topology.isolation_domains[{index}]",
                context={"domain": domain},
            )
        domains.append(domain)
    if not domains:
        fail(
            "INVALID_TOPOLOGY",
            "At least one isolation domain is required.",
            field_path="topology.isolation_domains",
        )

    services_raw = _require_list(
        raw.get("services"),
        "topology.services",
        "INVALID_TOPOLOGY",
    )
    if not services_raw:
        fail(
            "INVALID_TOPOLOGY",
            "At least one service is required.",
            field_path="topology.services",
        )

    domain_set = set(domains)
    service_names: set[str] = set()
    instance_ids: set[str] = set()
    services: dict[str, Service] = {}
    upstream_by_service: dict[str, list[str]] = {}

    for service_index, item in enumerate(services_raw):
        base_path = f"topology.services[{service_index}]"
        service_raw = _require_object(item, base_path, "INVALID_TOPOLOGY")
        name = _require_string(
            service_raw.get("name"),
            f"{base_path}.name",
            "INVALID_TOPOLOGY",
        )
        if name in service_names:
            fail(
                "INVALID_TOPOLOGY",
                "Service names must be unique.",
                field_path=f"{base_path}.name",
                context={"service": name},
            )
        service_names.add(name)

        domain = _require_string(
            service_raw.get("domain"),
            f"{base_path}.domain",
            "INVALID_TOPOLOGY",
        )
        if domain not in domain_set:
            fail(
                "INVALID_TOPOLOGY",
                "Service references an undeclared isolation domain.",
                field_path=f"{base_path}.domain",
                context={"domain": domain, "service": name},
            )

        upstream_raw = service_raw.get("upstream", [])
        upstream_list = _require_list(
            upstream_raw,
            f"{base_path}.upstream",
            "INVALID_TOPOLOGY",
        )
        upstream: list[str] = []
        for upstream_index, upstream_item in enumerate(upstream_list):
            upstream_name = _require_string(
                upstream_item,
                f"{base_path}.upstream[{upstream_index}]",
                "INVALID_TOPOLOGY",
            )
            if upstream_name in upstream:
                fail(
                    "INVALID_TOPOLOGY",
                    "Upstream dependencies must be unique per service.",
                    field_path=f"{base_path}.upstream[{upstream_index}]",
                    context={"service": name},
                )
            if upstream_name == name:
                fail(
                    "INVALID_TOPOLOGY_CYCLE",
                    "A service cannot depend on itself.",
                    field_path=f"{base_path}.upstream[{upstream_index}]",
                    context={"service": name},
                )
            upstream.append(upstream_name)
        upstream_by_service[name] = upstream

        service_weight_raw = service_raw.get("health_weight", 1.0)
        if not is_finite_number(service_weight_raw) or float(service_weight_raw) <= 0:
            fail(
                "INVALID_TOPOLOGY",
                "Service health weight must be a finite positive number.",
                field_path=f"{base_path}.health_weight",
                context={"service": name},
            )
        service_weight = float(service_weight_raw)

        data_seed = service_raw.get("data_seed")
        if not is_int(data_seed):
            fail(
                "INVALID_TOPOLOGY",
                "Service data_seed must be an integer.",
                field_path=f"{base_path}.data_seed",
                context={"service": name},
            )
        row_count = _require_nonnegative_int(
            service_raw.get("row_count", 16),
            f"{base_path}.row_count",
            "INVALID_TOPOLOGY",
        )

        instances_raw = _require_list(
            service_raw.get("instances"),
            f"{base_path}.instances",
            "INVALID_TOPOLOGY",
        )
        if not instances_raw:
            fail(
                "INVALID_TOPOLOGY",
                "A service must have at least one instance.",
                field_path=f"{base_path}.instances",
                context={"service": name},
            )
        instances: list[Instance] = []
        service_instance_ids: list[str] = []
        for instance_index, instance_item in enumerate(instances_raw):
            instance_path = f"{base_path}.instances[{instance_index}]"
            instance_raw = _require_object(
                instance_item,
                instance_path,
                "INVALID_TOPOLOGY",
            )
            instance_id = _require_string(
                instance_raw.get("instance_id"),
                f"{instance_path}.instance_id",
                "INVALID_TOPOLOGY",
            )
            if instance_id in instance_ids:
                fail(
                    "INVALID_TOPOLOGY",
                    "Instance IDs must be globally unique.",
                    field_path=f"{instance_path}.instance_id",
                    context={"instance": instance_id, "service": name},
                )
            instance_ids.add(instance_id)
            service_instance_ids.append(instance_id)

            weight_raw = instance_raw.get("health_weight", service_weight)
            if not is_finite_number(weight_raw) or float(weight_raw) <= 0:
                fail(
                    "INVALID_TOPOLOGY",
                    "Instance health weight must be a finite positive number.",
                    field_path=f"{instance_path}.health_weight",
                    context={"instance": instance_id, "service": name},
                )
            replica_seed = instance_raw.get("replica_seed")
            if not is_int(replica_seed):
                fail(
                    "INVALID_TOPOLOGY",
                    "Replica seed must be an integer.",
                    field_path=f"{instance_path}.replica_seed",
                    context={"instance": instance_id, "service": name},
                )
            data_variant = _require_nonnegative_int(
                instance_raw.get("data_variant", 0),
                f"{instance_path}.data_variant",
                "INVALID_TOPOLOGY",
            )
            instances.append(
                Instance(
                    instance_id=instance_id,
                    health_weight=float(weight_raw),
                    replica_seed=replica_seed,
                    data_variant=data_variant,
                )
            )

        initial_primary_raw = service_raw.get(
            "initial_primary",
            service_instance_ids[0],
        )
        initial_primary = _require_string(
            initial_primary_raw,
            f"{base_path}.initial_primary",
            "INVALID_TOPOLOGY",
        )
        if initial_primary not in service_instance_ids:
            fail(
                "INVALID_TOPOLOGY",
                "Initial primary must reference an instance of the same service.",
                field_path=f"{base_path}.initial_primary",
                context={"instance": initial_primary, "service": name},
            )

        services[name] = Service(
            name=name,
            domain=domain,
            upstream=tuple(upstream),
            health_weight=service_weight,
            data_seed=data_seed,
            row_count=row_count,
            initial_primary=initial_primary,
            instances=tuple(instances),
        )

    for service_name, upstream in upstream_by_service.items():
        for upstream_name in upstream:
            if upstream_name not in service_names:
                fail(
                    "INVALID_TOPOLOGY",
                    "Upstream dependency references an undeclared service.",
                    field_path=(
                        f"topology.services[{list(services).index(service_name)}]"
                        ".upstream"
                    ),
                    context={"service": service_name, "upstream": upstream_name},
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(service_name: str, stack: list[str]) -> None:
        if service_name in visiting:
            cycle = stack[stack.index(service_name) :] + [service_name]
            fail(
                "INVALID_TOPOLOGY_CYCLE",
                "Upstream dependency graph contains a cycle.",
                field_path=(
                    f"topology.services[{list(services).index(service_name)}]"
                    ".upstream"
                ),
                context={"cycle": cycle, "service": service_name},
            )
        if service_name in visited:
            return
        visiting.add(service_name)
        for upstream_name in upstream_by_service[service_name]:
            visit(upstream_name, stack + [service_name])
        visiting.remove(service_name)
        visited.add(service_name)

    for service_name in services:
        visit(service_name, [])

    return tuple(domains), services


def _validate_policy(
    raw: dict[str, Any],
    domains: tuple[str, ...],
    services: dict[str, Service],
) -> Policy:
    version = _require_positive_int(
        raw.get("version"),
        "policy.version",
        "INVALID_POLICY",
    )
    freeze_order_raw = _require_list(
        raw.get("freeze_order"),
        "policy.freeze_order",
        "FREEZE_ORDER_CONFLICT",
    )
    freeze_order: list[str] = []
    for index, item in enumerate(freeze_order_raw):
        domain = _require_string(
            item,
            f"policy.freeze_order[{index}]",
            "FREEZE_ORDER_CONFLICT",
        )
        if domain in freeze_order:
            fail(
                "FREEZE_ORDER_CONFLICT",
                "Freeze order contains a duplicate domain.",
                field_path=f"policy.freeze_order[{index}]",
                context={"domain": domain},
            )
        freeze_order.append(domain)

    declared = set(domains)
    ordered = set(freeze_order)
    for domain in domains:
        if domain not in ordered:
            fail(
                "FREEZE_ORDER_CONFLICT",
                "Freeze order must contain every declared isolation domain.",
                field_path="policy.freeze_order",
                context={"domain": domain},
            )
    for domain in freeze_order:
        if domain not in declared:
            fail(
                "FREEZE_ORDER_CONFLICT",
                "Freeze order references an undeclared isolation domain.",
                field_path="policy.freeze_order",
                context={"domain": domain},
            )

    freeze_batch_size = _require_positive_int(
        raw.get("freeze_batch_size", 1),
        "policy.freeze_batch_size",
    )
    max_concurrency = _require_positive_int(
        raw.get("max_concurrency", 4),
        "policy.max_concurrency",
    )
    time_budget_ms = _require_nonnegative_int(
        raw.get("time_budget_ms", 1000),
        "policy.time_budget_ms",
    )
    clock_tolerance = _require_nonnegative_int(
        raw.get("clock_regression_tolerance_ms", 0),
        "policy.clock_regression_tolerance_ms",
    )
    probe_costs_raw = raw.get("probe_cost_ms", {})
    if not isinstance(probe_costs_raw, dict):
        fail(
            "INVALID_POLICY",
            "probe_cost_ms must be an object.",
            field_path="policy.probe_cost_ms",
        )
    freeze_cost = _require_nonnegative_int(
        probe_costs_raw.get("freeze", 10),
        "policy.probe_cost_ms.freeze",
    )
    switch_cost = _require_nonnegative_int(
        probe_costs_raw.get("switch", 10),
        "policy.probe_cost_ms.switch",
    )

    policies_raw = _require_object(
        raw.get("services"),
        "policy.services",
        "INVALID_POLICY",
    )
    policies: dict[str, ServicePolicy] = {}
    for service_name, service in services.items():
        path = f"policy.services.{service_name}"
        if service_name not in policies_raw:
            fail(
                "INVALID_POLICY",
                "Policy is missing a service.",
                field_path=path,
                context={"service": service_name, "domain": service.domain},
            )
        item = _require_object(policies_raw[service_name], path, "INVALID_POLICY")
        mode = _require_string(
            item.get("mode"),
            f"{path}.mode",
            "INVALID_POLICY",
        )
        if mode not in VALID_MODES:
            fail(
                "INVALID_POLICY",
                "Switch mode must be active_passive or dual_active.",
                field_path=f"{path}.mode",
                context={"service": service_name, "domain": service.domain},
            )
        trigger_metric = _require_string(
            item.get("trigger_metric", "error_rate"),
            f"{path}.trigger_metric",
            "INVALID_POLICY",
        )
        trigger_threshold = _require_nonnegative_int(
            item.get("trigger_threshold"),
            f"{path}.trigger_threshold",
        )
        cooldown_ms = _require_nonnegative_int(
            item.get("cooldown_ms"),
            f"{path}.cooldown_ms",
        )
        ladder = _parse_ladder(
            item.get("failback_ladder"),
            f"{path}.failback_ladder",
            service_name,
            service.domain,
        )
        policies[service_name] = ServicePolicy(
            mode=mode,
            trigger_metric=trigger_metric,
            trigger_threshold=trigger_threshold,
            cooldown_ms=cooldown_ms,
            failback_ladder=ladder,
        )

    for service_name in policies_raw:
        if service_name not in services:
            fail(
                "INVALID_POLICY",
                "Policy references an undeclared service.",
                field_path=f"policy.services.{service_name}",
                context={"service": service_name},
            )

    return Policy(
        version=version,
        freeze_order=tuple(freeze_order),
        freeze_batch_size=freeze_batch_size,
        max_concurrency=max_concurrency,
        time_budget_ms=time_budget_ms,
        clock_regression_tolerance_ms=clock_tolerance,
        freeze_probe_cost_ms=freeze_cost,
        switch_probe_cost_ms=switch_cost,
        services=policies,
    )


def _validate_events(
    raw_events: list[Any],
    instance_ids: set[str],
) -> tuple[Event, ...]:
    events: list[Event] = []
    previous_seq: int | None = None
    previous_clock: int | None = None
    for index, item in enumerate(raw_events):
        path = f"events[{index}]"
        event_raw = _require_object(item, path, "INVALID_EVENT")
        seq = event_raw.get("seq")
        if not is_int(seq) or seq <= 0:
            fail(
                "INVALID_EVENT_SEQUENCE",
                "Event sequence must be a positive integer.",
                field_path=f"{path}.seq",
            )
        if previous_seq is not None and seq <= previous_seq:
            fail(
                "INVALID_EVENT_SEQUENCE",
                "Event sequence must be strictly increasing.",
                field_path=f"{path}.seq",
                context={"previous_seq": previous_seq, "seq": seq},
            )
        previous_seq = seq

        instance_id = _require_string(
            event_raw.get("instance_id"),
            f"{path}.instance_id",
            "INVALID_EVENT",
        )
        if instance_id not in instance_ids:
            fail(
                "INVALID_EVENT",
                "Event references an undeclared instance.",
                field_path=f"{path}.instance_id",
                context={"instance": instance_id},
            )
        metric = _require_string(
            event_raw.get("metric"),
            f"{path}.metric",
            "INVALID_EVENT",
        )
        value = event_raw.get("value")
        if not is_finite_number(value):
            fail(
                "INVALID_EVENT",
                "Metric value must be finite.",
                field_path=f"{path}.value",
                context={"instance": instance_id, "metric": metric},
            )
        window = _require_string(
            event_raw.get("window"),
            f"{path}.window",
            "INVALID_EVENT",
        )
        logical_time_ms = event_raw.get("logical_time_ms")
        if not is_int(logical_time_ms) or logical_time_ms < 0:
            fail(
                "INVALID_EVENT",
                "Event logical clock must be a non-negative integer.",
                field_path=f"{path}.logical_time_ms",
            )
        if previous_clock is not None and logical_time_ms < previous_clock:
            fail(
                "INVALID_EVENT",
                "Event logical clock must not move backwards in sequence order.",
                field_path=f"{path}.logical_time_ms",
                context={
                    "previous_logical_time_ms": previous_clock,
                    "logical_time_ms": logical_time_ms,
                },
            )
        previous_clock = logical_time_ms
        events.append(
            Event(
                seq=seq,
                instance_id=instance_id,
                metric=metric,
                value=value,
                window=window,
                logical_time_ms=logical_time_ms,
            )
        )
    return tuple(events)


def parse_sample(raw_value: Any) -> Sample:
    raw = _require_object(raw_value, "sample")
    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        fail(
            "INVALID_SAMPLE",
            "Only schema_version 1 is supported.",
            field_path="schema_version",
        )
    sample_id = _require_string(raw.get("sample_id"), "sample_id")
    topology_raw = _require_object(raw.get("topology"), "topology")
    policy_raw = _require_object(raw.get("policy"), "policy")
    events_container = raw.get("events")
    if isinstance(events_container, dict):
        events_raw = _require_list(events_container.get("events"), "events.events")
    else:
        events_raw = _require_list(events_container, "events")

    domains, services = _validate_topology(topology_raw)
    policy = _validate_policy(policy_raw, domains, services)
    instance_ids = {
        instance.instance_id
        for service in services.values()
        for instance in service.instances
    }
    events = _validate_events(events_raw, instance_ids)

    normalized_events = list(events_raw)
    normalized = {
        "events": normalized_events,
        "policy": policy_raw,
        "sample_id": sample_id,
        "schema_version": schema_version,
        "topology": topology_raw,
    }
    sample_digest = content_digest(normalized)
    policy_digest = content_digest(policy_raw)
    return Sample(
        sample_id=sample_id,
        raw=normalized,
        topology_raw=topology_raw,
        policy_raw=policy_raw,
        events_raw=normalized_events,
        domains=domains,
        services=services,
        policy=policy,
        events=events,
        sample_digest=sample_digest,
        policy_digest=policy_digest,
    )


def sample_from_stored(
    policy_json: str,
    sample_json: str,
) -> Sample:
    import json

    return parse_sample(json.loads(sample_json))

