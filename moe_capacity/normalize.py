"""Normalize the three supported service schemas to one byte-based model."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .model import (
    ChangeEvent,
    Expert,
    InferenceRequest,
    ModelValidationError,
    NormalizedProject,
    Replica,
)

MIB = 1024 * 1024
MB_DECIMAL = 1_000_000


class NormalizationError(ModelValidationError):
    """Raised for malformed or inconsistent schema input."""


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise NormalizationError(f"{context}: missing required field {key!r}")
    return mapping[key]


def _as_int(value: Any, context: str) -> int:
    if isinstance(value, bool):
        raise NormalizationError(f"{context}: expected integer, got bool")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"{context}: expected integer, got {value!r}") from exc
    if number != number.to_integral_value():
        raise NormalizationError(f"{context}: expected whole number, got {value!r}")
    return int(number)


def _decimal_bytes(value: Any, multiplier: int, context: str) -> int:
    try:
        number = Decimal(str(value)) * multiplier
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"{context}: invalid numeric value {value!r}") from exc
    if number < 0:
        raise NormalizationError(f"{context}: must not be negative")
    return int(number.to_integral_value())


def _unit_bytes(value: Any, unit: str, context: str) -> int:
    normalized = str(unit).strip().lower().replace(" ", "")
    if normalized in {"byte", "bytes", "b", "equiv_byte", "equiv_bytes"}:
        return _as_int(value, context)
    if normalized in {"mib", "mebibyte", "mebibytes"}:
        return _decimal_bytes(value, MIB, context)
    if normalized in {"mb", "megabyte", "megabytes"}:
        return _decimal_bytes(value, MB_DECIMAL, context)
    if normalized in {"kib", "kibibyte", "kibibytes"}:
        return _decimal_bytes(value, 1024, context)
    raise NormalizationError(f"{context}: unsupported unit {unit!r}")


def _struct_value(spec: Any, context: str, default_unit: str | None = None) -> int:
    if isinstance(spec, dict):
        value = _require(spec, "value", context)
        unit = spec.get("unit", default_unit)
        if unit is None:
            raise NormalizationError(f"{context}: missing unit")
        return _unit_bytes(value, unit, context)
    if default_unit is None:
        return _as_int(spec, context)
    return _unit_bytes(spec, default_unit, context)


def _precision_list(value: Any, context: str) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(",", "/").split("/")]
    elif isinstance(value, list):
        parts = [str(part).strip() for part in value]
    else:
        raise NormalizationError(f"{context}: expected string or list")
    result = tuple(part for part in parts if part)
    if not result:
        raise NormalizationError(f"{context}: precision list is empty")
    return result


def _domain_list(value: Any, context: str) -> tuple[str, ...]:
    if isinstance(value, str):
        result = tuple(part.strip() for part in value.split(",") if part.strip())
    elif isinstance(value, list):
        result = tuple(str(part).strip() for part in value if str(part).strip())
    else:
        raise NormalizationError(f"{context}: expected string or list")
    if not result:
        raise NormalizationError(f"{context}: domain list is empty")
    return result


def _detect_schema(document: dict[str, Any]) -> str:
    if document.get("schema_version") == "1.0" or (
        "units" in document and "experts" in document
    ):
        return "v1"
    schema = str(document.get("schema", ""))
    if schema.startswith("moe.service/v2") or "operations" in document:
        return "v2"
    fmt = str(document.get("format", ""))
    if fmt.startswith("moe-capacity/v3") or "topology" in document:
        return "v3"
    raise NormalizationError("cannot detect schema version")


def _common_settings(document: dict[str, Any], version: str) -> tuple[int, int]:
    if version == "v1":
        units = _require(document, "units", "v1")
        sample_bytes = _as_int(
            _require(units, "sample_equiv_bytes", "v1.units"),
            "v1.units.sample_equiv_bytes",
        )
        reference = _as_int(
            _require(units, "reference_sequence_length", "v1.units"),
            "v1.units.reference_sequence_length",
        )
    elif version == "v2":
        defaults = _require(document, "defaults", "v2")
        sample_bytes = _as_int(
            _require(defaults, "sample_equivalent_bytes", "v2.defaults"),
            "v2.defaults.sample_equivalent_bytes",
        )
        reference = _as_int(
            _require(defaults, "comm_reference_sequence_length", "v2.defaults"),
            "v2.defaults.comm_reference_sequence_length",
        )
    else:
        normalization = _require(document, "normalization", "v3")
        sample_bytes = _as_int(
            _require(normalization, "sample_equivalent_bytes", "v3.normalization"),
            "v3.normalization.sample_equivalent_bytes",
        )
        reference = _as_int(
            _require(normalization, "sequence_quantum", "v3.normalization"),
            "v3.normalization.sequence_quantum",
        )
    if sample_bytes <= 0 or reference <= 0:
        raise NormalizationError("sample equivalent bytes and sequence quantum must be > 0")
    return sample_bytes, reference


def _normalize_v1(
    document: dict[str, Any], sample_bytes: int, reference: int
) -> tuple[
    tuple[Replica, ...],
    tuple[Expert, ...],
    tuple[InferenceRequest, ...],
    tuple[ChangeEvent, ...],
    list[dict[str, Any]],
]:
    replicas: list[Replica] = []
    for index, item in enumerate(_require(document, "replicas", "v1")):
        context = f"v1.replicas[{index}]"
        replicas.append(
            Replica(
                replica_id=str(_require(item, "id", context)),
                capacity_bytes=_as_int(
                    _require(item, "capacity_bytes", context),
                    f"{context}.capacity_bytes",
                ),
                rack=str(_require(item, "rack", context)),
                supported_precisions=_precision_list(
                    _require(item, "precision", context), f"{context}.precision"
                ),
            )
        )

    experts: list[Expert] = []
    comm_evidence: list[list[Any]] = []
    for index, item in enumerate(_require(document, "experts", "v1")):
        context = f"v1.experts[{index}]"
        raw_mb = _require(item, "cross_replica_comm_mb", context)
        comm_bytes = _decimal_bytes(raw_mb, MB_DECIMAL, f"{context}.cross_replica_comm_mb")
        expert = Expert(
            expert_id=str(_require(item, "id", context)),
            home_replica=str(_require(item, "replica_id", context)),
            domains=_domain_list(_require(item, "domain_tags", context), f"{context}.domain_tags"),
            per_sample_cost_bytes=_as_int(
                _require(item, "cost_per_sample", context),
                f"{context}.cost_per_sample",
            ),
            cross_rack_comm_bytes=comm_bytes,
            precision=str(_require(item, "precision", context)),
        )
        experts.append(expert)
        comm_evidence.append([expert.expert_id, str(raw_mb), comm_bytes])

    requests: list[InferenceRequest] = []
    for index, item in enumerate(_require(document, "requests", "v1")):
        context = f"v1.requests[{index}]"
        samples = _as_int(
            _require(item, "sample_count", context), f"{context}.sample_count"
        )
        requests.append(
            InferenceRequest(
                request_id=str(_require(item, "request_id", context)),
                arrival_time=_as_int(
                    _require(item, "logical_time", context), f"{context}.logical_time"
                ),
                domain=str(_require(item, "domain", context)),
                sample_units=samples,
                sequence_length=_as_int(
                    _require(item, "sequence_length", context),
                    f"{context}.sequence_length",
                ),
                equivalent_bytes=samples * sample_bytes,
            )
        )

    events: list[ChangeEvent] = []
    for index, item in enumerate(document.get("events", [])):
        context = f"v1.events[{index}]"
        kind = str(_require(item, "type", context))
        if kind == "migrate_expert":
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "event_id", context)),
                    at=_as_int(
                        _require(item, "logical_time", context),
                        f"{context}.logical_time",
                    ),
                    kind="migrate_expert",
                    expert_id=str(_require(item, "expert_id", context)),
                    from_replica=str(_require(item, "from_replica_id", context)),
                    to_replica=str(_require(item, "to_replica_id", context)),
                )
            )
        elif kind == "scale_replica":
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "event_id", context)),
                    at=_as_int(
                        _require(item, "logical_time", context),
                        f"{context}.logical_time",
                    ),
                    kind="scale_replica",
                    replica_id=str(_require(item, "replica_id", context)),
                    new_capacity_bytes=_as_int(
                        _require(item, "capacity_bytes", context),
                        f"{context}.capacity_bytes",
                    ),
                )
            )
        else:
            raise NormalizationError(f"{context}: unsupported event type {kind!r}")

    evidence = [
        {
            "aspect": "expert_table_layout",
            "canonical_representation": "flat expert records with home_replica",
            "source_representation": "flat experts list",
            "source_schema": "v1",
            "stage": "normalization",
        },
        {
            "aspect": "request_load_unit",
            "canonical_representation": "sample_units plus equivalent_bytes",
            "source_representation": (
                f"integer sample_count per request (1 sample = {sample_bytes} "
                "equivalent bytes)"
            ),
            "source_schema": "v1",
            "stage": "normalization",
        },
        {
            "aspect": "communication_units",
            "canonical_representation": "bytes; only cross-rack dispatch is billed",
            "source_representation": comm_evidence,
            "source_schema": "v1",
            "stage": "normalization",
        },
        {
            "aspect": "capacity_unit",
            "canonical_representation": "weighted load bytes",
            "source_representation": "capacity_bytes",
            "source_schema": "v1",
            "stage": "normalization",
        },
        {
            "aspect": "precision",
            "canonical_representation": "supported_precisions tuple",
            "source_representation": "replica.precision string (may list profiles)",
            "source_schema": "v1",
            "stage": "normalization",
        },
    ]
    return tuple(replicas), tuple(experts), tuple(requests), tuple(events), evidence


def _normalize_v2(
    document: dict[str, Any], sample_bytes: int, reference: int
) -> tuple[
    tuple[Replica, ...],
    tuple[Expert, ...],
    tuple[InferenceRequest, ...],
    tuple[ChangeEvent, ...],
    list[dict[str, Any]],
]:
    replicas: list[Replica] = []
    experts: list[Expert] = []
    comm_evidence: list[list[Any]] = []
    for index, item in enumerate(_require(document, "replicas", "v2")):
        context = f"v2.replicas[{index}]"
        replica_id = str(_require(item, "replica_id", context))
        replicas.append(
            Replica(
                replica_id=replica_id,
                capacity_bytes=_struct_value(
                    _require(item, "capacity", context), f"{context}.capacity"
                ),
                rack=str(_require(item, "rack_id", context)),
                supported_precisions=_precision_list(
                    _require(item, "supported_precisions", context),
                    f"{context}.supported_precisions",
                ),
            )
        )
        for expert_index, nested in enumerate(item.get("experts", [])):
            ectx = f"{context}.experts[{expert_index}]"
            comm_spec = _require(nested, "cross_rack_comm", ectx)
            comm_bytes = _struct_value(comm_spec, f"{ectx}.cross_rack_comm")
            raw_value = comm_spec.get("value") if isinstance(comm_spec, dict) else comm_spec
            raw_unit = comm_spec.get("unit", "") if isinstance(comm_spec, dict) else ""
            expert = Expert(
                expert_id=str(_require(nested, "expert_id", ectx)),
                home_replica=replica_id,
                domains=_domain_list(
                    _require(nested, "domains", ectx), f"{ectx}.domains"
                ),
                per_sample_cost_bytes=_struct_value(
                    _require(nested, "sample_cost", ectx),
                    f"{ectx}.sample_cost",
                ),
                cross_rack_comm_bytes=comm_bytes,
                precision=str(_require(nested, "precision", ectx)),
            )
            experts.append(expert)
            unit_text = f"{raw_value} {raw_unit}".strip()
            comm_evidence.append([expert.expert_id, unit_text, comm_bytes])

    requests: list[InferenceRequest] = []
    for index, item in enumerate(_require(document, "requests", "v2")):
        context = f"v2.requests[{index}]"
        payload = _as_int(
            _require(item, "payload_bytes", context), f"{context}.payload_bytes"
        )
        if payload % sample_bytes:
            raise NormalizationError(
                f"{context}.payload_bytes={payload} is not divisible by "
                f"sample equivalent bytes {sample_bytes}"
            )
        samples = payload // sample_bytes
        requests.append(
            InferenceRequest(
                request_id=str(_require(item, "id", context)),
                arrival_time=_as_int(
                    _require(item, "at", context), f"{context}.at"
                ),
                domain=str(_require(item, "field", context)),
                sample_units=samples,
                sequence_length=_as_int(
                    _require(item, "sequence_length", context),
                    f"{context}.sequence_length",
                ),
                equivalent_bytes=samples * sample_bytes,
            )
        )

    events: list[ChangeEvent] = []
    for index, item in enumerate(document.get("operations", [])):
        context = f"v2.operations[{index}]"
        operation = str(_require(item, "op", context))
        if operation == "move_expert":
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "operation_id", context)),
                    at=_as_int(_require(item, "at", context), f"{context}.at"),
                    kind="migrate_expert",
                    expert_id=str(_require(item, "expert_id", context)),
                    from_replica=str(_require(item, "source_replica_id", context)),
                    to_replica=str(_require(item, "target_replica_id", context)),
                )
            )
        elif operation == "resize_replica":
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "operation_id", context)),
                    at=_as_int(_require(item, "at", context), f"{context}.at"),
                    kind="scale_replica",
                    replica_id=str(_require(item, "replica_id", context)),
                    new_capacity_bytes=_struct_value(
                        _require(item, "capacity", context), f"{context}.capacity"
                    ),
                )
            )
        else:
            raise NormalizationError(f"{context}: unsupported operation {operation!r}")

    evidence = [
        {
            "aspect": "expert_table_layout",
            "canonical_representation": "flat expert records with home_replica",
            "source_representation": "experts nested under replicas",
            "source_schema": "v2",
            "stage": "normalization",
        },
        {
            "aspect": "request_load_unit",
            "canonical_representation": "sample_units plus equivalent_bytes",
            "source_representation": (
                "equivalent payload_bytes divided by sample_equivalent_bytes"
            ),
            "source_schema": "v2",
            "stage": "normalization",
        },
        {
            "aspect": "communication_units",
            "canonical_representation": "bytes; only cross-rack dispatch is billed",
            "source_representation": comm_evidence,
            "source_schema": "v2",
            "stage": "normalization",
        },
        {
            "aspect": "capacity_unit",
            "canonical_representation": "weighted load bytes",
            "source_representation": "structured capacity {value, unit}",
            "source_schema": "v2",
            "stage": "normalization",
        },
        {
            "aspect": "precision",
            "canonical_representation": "supported_precisions tuple",
            "source_representation": "supported_precisions list",
            "source_schema": "v2",
            "stage": "normalization",
        },
    ]
    return tuple(replicas), tuple(experts), tuple(requests), tuple(events), evidence


def _normalize_v3(
    document: dict[str, Any], sample_bytes: int, reference: int
) -> tuple[
    tuple[Replica, ...],
    tuple[Expert, ...],
    tuple[InferenceRequest, ...],
    tuple[ChangeEvent, ...],
    list[dict[str, Any]],
]:
    replicas: list[Replica] = []
    experts: list[Expert] = []
    comm_evidence: list[list[Any]] = []
    topology = _require(document, "topology", "v3")
    nodes = _require(topology, "nodes", "v3.topology")
    for index, item in enumerate(nodes):
        context = f"v3.topology.nodes[{index}]"
        replica_id = str(_require(item, "worker", context))
        limits = _require(item, "limits", context)
        replicas.append(
            Replica(
                replica_id=replica_id,
                capacity_bytes=_as_int(
                    _require(limits, "weighted_load_bytes", f"{context}.limits"),
                    f"{context}.limits.weighted_load_bytes",
                ),
                rack=str(_require(_require(item, "rack", context), "id", context)),
                supported_precisions=_precision_list(
                    _require(item, "precision_profiles", context),
                    f"{context}.precision_profiles",
                ),
            )
        )
        for branch_index, nested in enumerate(item.get("branches", [])):
            bctx = f"{context}.branches[{branch_index}]"
            cost = _require(nested, "cost", bctx)
            egress = _require(nested, "egress", bctx)
            raw_egress = _require(egress, "mebibytes_per_round", f"{bctx}.egress")
            comm_bytes = _decimal_bytes(
                raw_egress, MIB, f"{bctx}.egress.mebibytes_per_round"
            )
            expert = Expert(
                expert_id=str(_require(nested, "shard", bctx)),
                home_replica=replica_id,
                domains=_domain_list(
                    _require(nested, "domains", bctx), f"{bctx}.domains"
                ),
                per_sample_cost_bytes=_as_int(
                    _require(cost, "bytes_per_sample", bctx),
                    f"{bctx}.cost.bytes_per_sample",
                ),
                cross_rack_comm_bytes=comm_bytes,
                precision=str(_require(nested, "numeric_mode", bctx)),
            )
            experts.append(expert)
            comm_evidence.append(
                [expert.expert_id, f"{raw_egress} MiB", comm_bytes]
            )

    requests: list[InferenceRequest] = []
    for index, item in enumerate(_require(document, "inference", "v3")):
        context = f"v3.inference[{index}]"
        work = _require(item, "work", context)
        if "samples" in work:
            samples = _as_int(work["samples"], f"{context}.work.samples")
        elif "equivalent_bytes" in work:
            equivalent = _as_int(
                work["equivalent_bytes"], f"{context}.work.equivalent_bytes"
            )
            if equivalent % sample_bytes:
                raise NormalizationError(
                    f"{context}.work.equivalent_bytes is not divisible by "
                    f"{sample_bytes}"
                )
            samples = equivalent // sample_bytes
        else:
            raise NormalizationError(f"{context}.work: missing samples/equivalent_bytes")
        requests.append(
            InferenceRequest(
                request_id=str(_require(item, "rid", context)),
                arrival_time=_as_int(
                    _require(item, "tick", context), f"{context}.tick"
                ),
                domain=str(_require(item, "topic", context)),
                sample_units=samples,
                sequence_length=_as_int(
                    _require(item, "context_tokens", context),
                    f"{context}.context_tokens",
                ),
                equivalent_bytes=samples * sample_bytes,
            )
        )

    events: list[ChangeEvent] = []
    for index, item in enumerate(document.get("changes", [])):
        context = f"v3.changes[{index}]"
        operation = str(_require(item, "op", context))
        if operation == "relocate_shard":
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "change_id", context)),
                    at=_as_int(_require(item, "tick", context), f"{context}.tick"),
                    kind="migrate_expert",
                    expert_id=str(_require(item, "shard", context)),
                    from_replica=str(_require(item, "origin", context)),
                    to_replica=str(_require(item, "destination", context)),
                )
            )
        elif operation == "resize_worker":
            capacity = _require(item, "capacity", context)
            events.append(
                ChangeEvent(
                    event_id=str(_require(item, "change_id", context)),
                    at=_as_int(_require(item, "tick", context), f"{context}.tick"),
                    kind="scale_replica",
                    replica_id=str(_require(item, "worker", context)),
                    new_capacity_bytes=_as_int(
                        _require(capacity, "weighted_load_bytes", f"{context}.capacity"),
                        f"{context}.capacity.weighted_load_bytes",
                    ),
                )
            )
        else:
            raise NormalizationError(f"{context}: unsupported change {operation!r}")

    evidence = [
        {
            "aspect": "expert_table_layout",
            "canonical_representation": "flat expert records with home_replica",
            "source_representation": "topology nodes with nested branches",
            "source_schema": "v3",
            "stage": "normalization",
        },
        {
            "aspect": "request_load_unit",
            "canonical_representation": "sample_units plus equivalent_bytes",
            "source_representation": (
                "work.samples (or equivalent_bytes divided by "
                "sample_equivalent_bytes)"
            ),
            "source_schema": "v3",
            "stage": "normalization",
        },
        {
            "aspect": "communication_units",
            "canonical_representation": "bytes; only cross-rack dispatch is billed",
            "source_representation": comm_evidence,
            "source_schema": "v3",
            "stage": "normalization",
        },
        {
            "aspect": "capacity_unit",
            "canonical_representation": "weighted load bytes",
            "source_representation": "limits.weighted_load_bytes",
            "source_schema": "v3",
            "stage": "normalization",
        },
        {
            "aspect": "precision",
            "canonical_representation": "supported_precisions tuple",
            "source_representation": "precision_profiles",
            "source_schema": "v3",
            "stage": "normalization",
        },
    ]
    return tuple(replicas), tuple(experts), tuple(requests), tuple(events), evidence


def _validate_relationships(project: NormalizedProject) -> None:
    replicas, experts, requests = project.by_id()
    if len(replicas) != len(project.replicas):
        raise NormalizationError("duplicate replica id")
    if len(experts) != len(project.experts):
        raise NormalizationError("duplicate expert id")
    if len(requests) != len(project.requests):
        raise NormalizationError("duplicate request id")
    for replica in project.replicas:
        if replica.capacity_bytes <= 0:
            raise NormalizationError(
                f"replica {replica.replica_id}: capacity must be positive"
            )
        if not replica.supported_precisions:
            raise NormalizationError(
                f"replica {replica.replica_id}: no supported precision"
            )
    for expert in project.experts:
        if expert.home_replica not in replicas:
            raise NormalizationError(
                f"expert {expert.expert_id}: unknown home replica {expert.home_replica}"
            )
        if expert.per_sample_cost_bytes <= 0:
            raise NormalizationError(
                f"expert {expert.expert_id}: sample cost must be positive"
            )
        if expert.cross_rack_comm_bytes < 0:
            raise NormalizationError(
                f"expert {expert.expert_id}: communication must not be negative"
            )
        if expert.precision not in replicas[expert.home_replica].supported_precisions:
            raise NormalizationError(
                f"expert {expert.expert_id}: home replica does not support "
                f"{expert.precision}"
            )
    for request in project.requests:
        if request.sample_units <= 0:
            raise NormalizationError(
                f"request {request.request_id}: sample units must be positive"
            )
        if request.sequence_length <= 0:
            raise NormalizationError(
                f"request {request.request_id}: sequence length must be positive"
            )
        if request.equivalent_bytes != request.sample_units * project.normalization[
            "sample_equivalent_bytes"
        ]:
            raise NormalizationError(
                f"request {request.request_id}: equivalent byte mismatch"
            )
        if not any(request.domain in expert.domains for expert in project.experts):
            raise NormalizationError(
                f"request {request.request_id}: no expert declares domain "
                f"{request.domain!r}"
            )
    for event in project.events:
        if event.kind == "migrate_expert":
            if event.expert_id not in experts:
                raise NormalizationError(
                    f"event {event.event_id}: unknown expert {event.expert_id}"
                )
            if event.to_replica not in replicas:
                raise NormalizationError(
                    f"event {event.event_id}: unknown replica {event.to_replica}"
                )
        elif event.kind == "scale_replica":
            if event.replica_id not in replicas:
                raise NormalizationError(
                    f"event {event.event_id}: unknown replica {event.replica_id}"
                )
            if not event.new_capacity_bytes or event.new_capacity_bytes <= 0:
                raise NormalizationError(
                    f"event {event.event_id}: capacity must be positive"
                )


def normalize_document(
    document: dict[str, Any],
    *,
    source_path: str | None = None,
) -> NormalizedProject:
    """Normalize one v1/v2/v3 document into :class:`NormalizedProject`."""

    if not isinstance(document, dict):
        raise NormalizationError("schema root must be a JSON object")
    version = _detect_schema(document)
    sample_bytes, reference = _common_settings(document, version)
    project_name = str(_require(document, "project", version))
    if version == "v1":
        replicas, experts, requests, events, evidence = _normalize_v1(
            document, sample_bytes, reference
        )
    elif version == "v2":
        replicas, experts, requests, events, evidence = _normalize_v2(
            document, sample_bytes, reference
        )
    else:
        replicas, experts, requests, events, evidence = _normalize_v3(
            document, sample_bytes, reference
        )
    normalized = NormalizedProject(
        project_name=project_name,
        source_schema=version,
        replicas=replicas,
        experts=experts,
        requests=requests,
        events=events,
        normalization={
            "communication_unit": "bytes",
            "cross_rack_only": True,
            "load_unit": "equivalent_bytes",
            "reference_sequence_length": reference,
            "sample_equivalent_bytes": sample_bytes,
        },
        normalization_evidence=tuple(evidence),
        source_path=source_path,
    )
    _validate_relationships(normalized)
    return normalized


def normalize_file(path: str | Path) -> NormalizedProject:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"cannot read schema {source}: {exc}") from exc
    return normalize_document(document, source_path=source.name)


def canonical_json(project: NormalizedProject | dict[str, Any]) -> str:
    payload = project.canonical_dict() if isinstance(project, NormalizedProject) else project
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_fingerprint(project: NormalizedProject | dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(project).encode("utf-8")).hexdigest()


def sequence_blocks(sequence_length: int, reference_length: int) -> int:
    return max(1, math.ceil(sequence_length / reference_length))

