from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable

from .jsonutil import canonical_decimal, hash_json


PARAM_ORDER: tuple[str, ...] = (
    "timeout_ms",
    "retry_multiplier",
    "sample_rate",
    "capacity",
    "weight",
)

DERIVED_ORDER: tuple[str, ...] = (
    "worst_chain_timeout",
    "effective_capacity",
    "weighted_sample_rate",
)

PARAM_FOR_DERIVED: dict[str, tuple[str, ...]] = {
    "worst_chain_timeout": ("timeout_ms", "retry_multiplier"),
    "effective_capacity": ("capacity",),
    "weighted_sample_rate": ("sample_rate", "weight"),
}

DEFAULT_DERIVED_SCALES: dict[str, int] = {
    "worst_chain_timeout": 6,
    "effective_capacity": 0,
    "weighted_sample_rate": 12,
}


@dataclass(frozen=True)
class ParamSpec:
    scale: int
    error_budget: Fraction


@dataclass(frozen=True)
class LedgerConfig:
    parameters: dict[str, ParamSpec]
    derived_scales: dict[str, int]
    compare_tolerance: Fraction
    logical_clock_step: int
    max_concurrency: int
    truth_precision: int
    truth_rounding: str
    higher_scale_increment: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "compare_tolerance": canonical_decimal(self.compare_tolerance),
            "derived_scales": dict(sorted(self.derived_scales.items())),
            "higher_scale_increment": self.higher_scale_increment,
            "logical_clock_step": self.logical_clock_step,
            "max_concurrency": self.max_concurrency,
            "parameters": {
                name: {
                    "error_budget": canonical_decimal(spec.error_budget),
                    "scale": spec.scale,
                }
                for name, spec in sorted(self.parameters.items())
            },
            "truth_precision": self.truth_precision,
            "truth_rounding": self.truth_rounding,
        }


@dataclass(frozen=True)
class ServiceDef:
    name: str
    layer: str
    depends_on: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "depends_on": list(self.depends_on),
            "layer": self.layer,
            "name": self.name,
        }


@dataclass(frozen=True)
class Snapshot:
    services: tuple[ServiceDef, ...]
    layers: dict[str, dict[str, Fraction]]
    config: LedgerConfig
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "layers": {
                layer: {
                    name: canonical_decimal(value)
                    for name, value in sorted(values.items())
                }
                for layer, values in sorted(self.layers.items())
            },
            "services": [service.to_dict() for service in self.services],
        }

    @property
    def version(self) -> str:
        return "v-" + hash_json(self.to_dict())

    @property
    def signature(self) -> str:
        return "s-" + hashlib.sha256(
            (str(self.seed) + ":" + self.version).encode("utf-8")
        ).hexdigest()

    def service_map(self) -> dict[str, ServiceDef]:
        return {service.name: service for service in self.services}

    def topological_order(self) -> list[str]:
        return topological_order(self.services)

    def ancestors(self) -> dict[str, set[str]]:
        by_name = self.service_map()
        memo: dict[str, set[str]] = {}

        def visit(name: str) -> set[str]:
            if name in memo:
                return memo[name]
            result = {name}
            for dependency in by_name[name].depends_on:
                result.update(visit(dependency))
            memo[name] = result
            return result

        for name in by_name:
            visit(name)
        return memo


@dataclass(frozen=True)
class Patch:
    patch_id: str
    operations: tuple[dict[str, Any], ...]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "operations": list(self.operations),
            "patch_id": self.patch_id,
        }

    @property
    def patch_hash(self) -> str:
        return hash_json(self.to_dict())


def topological_order(services: Iterable[ServiceDef]) -> list[str]:
    import heapq

    from .errors import LedgerError

    by_name = {service.name: service for service in services}
    indegree = {name: len(service.depends_on) for name, service in by_name.items()}
    downstream: dict[str, list[str]] = {name: [] for name in by_name}
    for service in by_name.values():
        for dependency in service.depends_on:
            downstream[dependency].append(service.name)

    ready = [name for name, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    result: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        result.append(name)
        for child in sorted(downstream[name]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(result) != len(by_name):
        cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
        raise LedgerError(
            "E_CYCLE",
            "dependency graph contains a cycle",
            {"services": cyclic},
        )
    return result


def snapshot_from_dict(payload: dict[str, Any], *, seed: int = 0) -> Snapshot:
    from .validation import parse_config, parse_layers, parse_topology

    topology_payload = {"services": payload["services"]}
    layers_payload = {"layers": payload["layers"]}
    config_payload = payload["config"]
    config = parse_config(config_payload)
    services = parse_topology(topology_payload)
    layers = parse_layers(layers_payload, services)
    return Snapshot(
        services=services,
        layers=layers,
        config=config,
        seed=seed,
    )
