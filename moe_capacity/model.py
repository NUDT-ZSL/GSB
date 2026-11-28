"""Canonical in-memory model shared by normalization, routing and verification."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


class ModelValidationError(ValueError):
    """Raised when a schema document cannot form a consistent canonical model."""


@dataclass(frozen=True)
class Replica:
    replica_id: str
    capacity_bytes: int
    rack: str
    supported_precisions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "capacity_bytes": self.capacity_bytes,
            "rack": self.rack,
            "replica_id": self.replica_id,
            "supported_precisions": list(self.supported_precisions),
        }


@dataclass(frozen=True)
class Expert:
    expert_id: str
    home_replica: str
    domains: tuple[str, ...]
    per_sample_cost_bytes: int
    cross_rack_comm_bytes: int
    precision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cross_rack_comm_bytes": self.cross_rack_comm_bytes,
            "domains": list(self.domains),
            "expert_id": self.expert_id,
            "home_replica": self.home_replica,
            "per_sample_cost_bytes": self.per_sample_cost_bytes,
            "precision": self.precision,
        }


@dataclass(frozen=True)
class InferenceRequest:
    request_id: str
    arrival_time: int
    domain: str
    sample_units: int
    sequence_length: int
    equivalent_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "arrival_time": self.arrival_time,
            "domain": self.domain,
            "equivalent_bytes": self.equivalent_bytes,
            "request_id": self.request_id,
            "sample_units": self.sample_units,
            "sequence_length": self.sequence_length,
        }


@dataclass(frozen=True)
class ChangeEvent:
    event_id: str
    at: int
    kind: str
    expert_id: str | None = None
    from_replica: str | None = None
    to_replica: str | None = None
    replica_id: str | None = None
    new_capacity_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "at": self.at,
            "event_id": self.event_id,
            "kind": self.kind,
        }
        if self.kind == "migrate_expert":
            out.update(
                {
                    "expert_id": self.expert_id,
                    "from_replica": self.from_replica,
                    "to_replica": self.to_replica,
                }
            )
        elif self.kind == "scale_replica":
            out.update(
                {
                    "new_capacity_bytes": self.new_capacity_bytes,
                    "replica_id": self.replica_id,
                }
            )
        else:
            raise ModelValidationError(f"unsupported event kind: {self.kind!r}")
        return out


@dataclass(frozen=True)
class NormalizedProject:
    project_name: str
    source_schema: str
    replicas: tuple[Replica, ...]
    experts: tuple[Expert, ...]
    requests: tuple[InferenceRequest, ...]
    events: tuple[ChangeEvent, ...]
    normalization: dict[str, Any]
    normalization_evidence: tuple[dict[str, Any], ...]
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "events": [event.to_dict() for event in self.events],
            "experts": [expert.to_dict() for expert in self.experts],
            "normalization": dict(self.normalization),
            "normalization_evidence": [
                dict(item) for item in self.normalization_evidence
            ],
            "project_name": self.project_name,
            "replicas": [replica.to_dict() for replica in self.replicas],
            "requests": [request.to_dict() for request in self.requests],
            "source_schema": self.source_schema,
        }
        if self.source_path is not None:
            out["source_path"] = self.source_path
        return out

    def canonical_dict(self) -> dict[str, Any]:
        """Return the source-independent representation used for fingerprints."""

        return {
            "events": [
                event.to_dict()
                for event in sorted(
                    self.events, key=lambda item: (item.at, item.event_id)
                )
            ],
            "experts": [
                expert.to_dict()
                for expert in sorted(self.experts, key=lambda item: item.expert_id)
            ],
            "normalization": dict(self.normalization),
            "project_name": self.project_name,
            "replicas": [
                replica.to_dict()
                for replica in sorted(
                    self.replicas, key=lambda item: item.replica_id
                )
            ],
            "requests": [
                request.to_dict()
                for request in sorted(
                    self.requests, key=lambda item: item.request_id
                )
            ],
        }

    def by_id(self) -> tuple[dict[str, Replica], dict[str, Expert], dict[str, InferenceRequest]]:
        return (
            {item.replica_id: item for item in self.replicas},
            {item.expert_id: item for item in self.experts},
            {item.request_id: item for item in self.requests},
        )


@dataclass(frozen=True)
class Topology:
    """A point-in-time topology used for routing.

    ``events_seen`` makes event-driven decisions traceable without leaking
    mutable state into the normalized input model.
    """

    replicas: dict[str, Replica]
    experts: dict[str, Expert]
    events_seen: tuple[str, ...] = ()

    def copy(self) -> "Topology":
        return Topology(
            replicas=dict(self.replicas),
            experts=dict(self.experts),
            events_seen=tuple(self.events_seen),
        )

    def apply_event(self, event: ChangeEvent) -> "Topology":
        replicas = dict(self.replicas)
        experts = dict(self.experts)
        if event.kind == "migrate_expert":
            assert event.expert_id is not None
            assert event.to_replica is not None
            expert = experts[event.expert_id]
            if event.from_replica is not None and expert.home_replica != event.from_replica:
                raise ModelValidationError(
                    f"event {event.event_id}: expert {expert.expert_id} is on "
                    f"{expert.home_replica}, not {event.from_replica}"
                )
            if event.to_replica not in replicas:
                raise ModelValidationError(
                    f"event {event.event_id}: unknown target replica {event.to_replica}"
                )
            experts[event.expert_id] = replace(expert, home_replica=event.to_replica)
            if expert.precision not in replicas[event.to_replica].supported_precisions:
                raise ModelValidationError(
                    f"event {event.event_id}: target replica {event.to_replica} "
                    f"does not support {expert.precision}"
                )
        elif event.kind == "scale_replica":
            assert event.replica_id is not None
            assert event.new_capacity_bytes is not None
            replica = replicas[event.replica_id]
            replicas[event.replica_id] = replace(
                replica, capacity_bytes=event.new_capacity_bytes
            )
        else:
            raise ModelValidationError(f"unsupported event kind: {event.kind!r}")
        return Topology(
            replicas=replicas,
            experts=experts,
            events_seen=tuple(self.events_seen) + (event.event_id,),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_seen": list(self.events_seen),
            "experts": [
                self.experts[key].to_dict() for key in sorted(self.experts)
            ],
            "replicas": [
                self.replicas[key].to_dict() for key in sorted(self.replicas)
            ],
        }

