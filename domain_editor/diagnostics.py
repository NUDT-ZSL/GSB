"""Cross-version roundtrip and diagnostic report generation."""

from __future__ import annotations

import copy
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from .artifacts import SerializationResult
from .config import units_per_second
from .degradation import compute_target_losses
from .injections import match_injections
from .jsonutil import dumps_canonical
from .model import Document, Failure, LossItem, ProjectState, decimal_str
from .v1 import parse_v1, serialize_v1
from .v2 import parse_v2, serialize_v2


def _base_documents(state: ProjectState) -> dict[str, Document]:
    result: dict[str, Document] = {}
    for doc_id in sorted(state.documents):
        doc = copy.deepcopy(state.documents[doc_id])
        doc.relations = []
        doc.events = []
        result[doc_id] = doc
    return result


def _entry_map(documents: dict[str, Document]) -> dict[tuple[str, str, str], Any]:
    result: dict[tuple[str, str, str], Any] = {}
    for doc_id in sorted(documents):
        for relation in documents[doc_id].relations:
            result[(doc_id, "relation", relation.entry_id)] = relation
        for event in documents[doc_id].events:
            result[(doc_id, "event", event.entry_id)] = event
    return result


def _histogram(values: list[Decimal], config: dict[str, Any]) -> list[dict[str, Any]]:
    configured = config.get("conversions", {}).get("diagnostic_bins")
    if configured:
        edges = [float(item) for item in configured]
    else:
        edges = [0.0, 1e-12, 1e-9, 1e-6, 1e-3, 0.01, 0.1, 1.0, 1e9]
    if not edges or edges[0] != 0.0:
        edges = [0.0, *edges]
    if edges[-1] < 1e8:
        edges.append(1e9)
    numeric = np.asarray([float(value) for value in values], dtype=np.float64)
    counts, bin_edges = np.histogram(numeric, bins=np.asarray(edges, dtype=np.float64))
    return [
        {
            "min_inclusive": decimal_str(Decimal(str(float(bin_edges[index])))),
            "max_exclusive": decimal_str(Decimal(str(float(bin_edges[index + 1])))),
            "count": int(counts[index]),
        }
        for index in range(len(counts))
    ]


def _position_pairs(entry: Any, kind: str):
    if kind == "relation":
        yield "head.start", entry.head.start
        yield "head.end", entry.head.end
        yield "tail.start", entry.tail.start
        yield "tail.end", entry.tail.end
    else:
        yield "span.start", entry.span.start
        yield "span.end", entry.span.end
        for index, argument in enumerate(entry.arguments):
            yield f"arguments[{index}].start", argument.span.start
            yield f"arguments[{index}].end", argument.span.end


def _compare_roundtrip(
    state: ProjectState,
    parsed_documents: dict[str, Document],
    parse_failures: list[Failure],
    serialization: SerializationResult,
    target_version: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    original_map = _entry_map(state.documents)
    parsed_map = _entry_map(parsed_documents)
    missing = sorted(set(original_map) - set(parsed_map))
    extra = sorted(set(parsed_map) - set(original_map))
    deviations: list[dict[str, Any]] = []
    position_values: list[Decimal] = []
    fields: dict[str, list[Decimal]] = {}
    confidence_deviation = Decimal(0)
    role_total = sum(
        len(entry.arguments)
        for entry in original_map.values()
        if hasattr(entry, "arguments")
    )
    role_restored = 0
    role_mismatches: list[dict[str, Any]] = []
    for key in sorted(set(original_map) & set(parsed_map)):
        doc_id, kind, entry_id = key
        original = original_map[key]
        parsed = parsed_map[key]
        position_pairs = list(_position_pairs(original, kind))
        parsed_positions = dict(_position_pairs(parsed, kind))
        for field_name, original_position in position_pairs:
            parsed_position = parsed_positions.get(field_name)
            if parsed_position is None:
                deviation = Decimal("Infinity")
            else:
                deviation = abs(parsed_position - original_position)
            position_values.append(deviation)
            fields.setdefault(field_name, []).append(deviation)
            deviations.append(
                {
                    "doc_id": doc_id,
                    "kind": kind,
                    "entry_id": entry_id,
                    "field": field_name,
                    "input": decimal_str(original_position),
                    "output": decimal_str(parsed_position) if parsed_position is not None else None,
                    "deviation": decimal_str(deviation) if deviation.is_finite() else "Infinity",
                }
            )
        confidence_deviation = max(confidence_deviation, abs(parsed.confidence - original.confidence))
        if kind == "event":
            for index, original_argument in enumerate(original.arguments):
                if index >= len(parsed.arguments):
                    role_mismatches.append(
                        {
                            "doc_id": doc_id,
                            "entry_id": entry_id,
                            "argument_index": index,
                            "input": original_argument.role,
                            "output": None,
                        }
                    )
                    continue
                output_role = parsed.arguments[index].role
                if output_role == original_argument.role:
                    role_restored += 1
                else:
                    role_mismatches.append(
                        {
                            "doc_id": doc_id,
                            "entry_id": entry_id,
                            "argument_index": index,
                            "input": original_argument.role,
                            "output": output_role,
                        }
                    )
            if len(parsed.arguments) > len(original.arguments):
                for index in range(len(original.arguments), len(parsed.arguments)):
                    role_mismatches.append(
                        {
                            "doc_id": doc_id,
                            "entry_id": entry_id,
                            "argument_index": index,
                            "input": None,
                            "output": parsed.arguments[index].role,
                        }
                    )
    for key in missing:
        doc_id, kind, entry_id = key
        if kind != "event":
            continue
        original = original_map[key]
        for index, original_argument in enumerate(original.arguments):
            role_mismatches.append(
                {
                    "doc_id": doc_id,
                    "entry_id": entry_id,
                    "argument_index": index,
                    "input": original_argument.role,
                    "output": None,
                }
            )
    max_position = max(position_values) if position_values else Decimal(0)
    losses = compute_target_losses(state, target_version, config, 1)
    irreversible = [loss for loss in losses if not loss.reversible]
    reversible = [loss for loss in losses if loss.reversible]
    entry_equivalent = (
        not missing
        and not extra
        and max_position == 0
        and confidence_deviation == 0
        and role_restored == role_total
        and not role_mismatches
        and not parse_failures
        and not serialization.failures
    )
    return {
        "target_version": target_version,
        "counts": {
            "input_relations": sum(len(doc.relations) for doc in state.documents.values()),
            "input_events": sum(len(doc.events) for doc in state.documents.values()),
            "output_relations": sum(len(doc.relations) for doc in parsed_documents.values()),
            "output_events": sum(len(doc.events) for doc in parsed_documents.values()),
            "matched_entries": len(set(original_map) & set(parsed_map)),
            "missing_entries": len(missing),
            "extra_entries": len(extra),
        },
        "missing_entries": [
            {"doc_id": key[0], "kind": key[1], "entry_id": key[2]} for key in missing
        ],
        "extra_entries": [
            {"doc_id": key[0], "kind": key[1], "entry_id": key[2]} for key in extra
        ],
        "position_deviations": deviations,
        "position_deviation_histogram": _histogram(position_values, config),
        "position_deviation_histogram_by_field": {
            field_name: _histogram(values, config) for field_name, values in sorted(fields.items())
        },
        "max_position_deviation": decimal_str(max_position) if max_position.is_finite() else "Infinity",
        "max_confidence_deviation": decimal_str(confidence_deviation),
        "role_restoration": {
            "total": role_total,
            "restored": role_restored,
            "failed": len(role_mismatches),
            "mismatches": role_mismatches,
            "all_restored": role_restored == role_total and not role_mismatches,
        },
        "entry_equivalent": entry_equivalent,
        "fully_equivalent": entry_equivalent and not irreversible and not losses,
        "serialization_failures": [failure.to_dict() for failure in serialization.failures],
        "parse_failures": [failure.to_dict() for failure in parse_failures],
        "reversible_losses": [loss.to_dict() for loss in reversible],
        "irreversible_losses": [loss.to_dict() for loss in irreversible],
    }


def _roundtrip_target(
    state: ProjectState,
    target_version: str,
    config: dict[str, Any],
    max_workers: int,
) -> dict[str, Any]:
    base_documents = _base_documents(state)
    if target_version == "v1":
        serialization = serialize_v1(state, config, max_workers)
    else:
        serialization = serialize_v2(state, config, max_workers)
    with tempfile.TemporaryDirectory(prefix="domain-diagnostics-") as temporary:
        path = Path(temporary) / ("project.v1.txt" if target_version == "v1" else "project.v2.json")
        path.write_text(serialization.text, encoding="utf-8")
        if target_version == "v1":
            parsed, failures = parse_v1(path, base_documents, config, max_workers)
        else:
            parsed, _, _, failures = parse_v2(path, base_documents, config, max_workers)
    return _compare_roundtrip(
        state,
        parsed,
        failures,
        serialization,
        target_version,
        config,
    )


def _code_summary(failures: list[Failure], extra_codes: list[str]) -> dict[str, int]:
    counter = Counter(failure.business_code for failure in failures)
    for code in extra_codes:
        counter[code] += 1
    return {code: int(counter[code]) for code in sorted(counter)}


def build_diagnostic_report(
    state: ProjectState,
    config: dict[str, Any],
    max_workers: int,
    injection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    v1_roundtrip = _roundtrip_target(state, "v1", config, max_workers)
    v2_roundtrip = _roundtrip_target(state, "v2", config, max_workers)
    losses = compute_target_losses(state, state.target_version, config, max_workers)
    reversible_losses = [loss.to_dict() for loss in losses if loss.reversible]
    irreversible_losses = [loss.to_dict() for loss in losses if not loss.reversible]
    conversion_changes: list[dict[str, Any]] = []
    if state.source_version == "v1":
        conversion_changes.extend(
            [
                {
                    "code": "V1_INTEGER_POSITION_TO_V2_SECONDS",
                    "reversible": True,
                    "detail": f"divide integer units by {decimal_str(units_per_second(config))}",
                },
                {
                    "code": "V1_PERMILLE_TO_V2_FLOAT",
                    "reversible": True,
                    "detail": "divide permille confidence by 1000",
                },
                {
                    "code": "ROLE_NAMING_TABLE_GENERATED",
                    "reversible": True,
                    "detail": "role keys are generated from the configured role table",
                },
                {
                    "code": "V1_ORIGINAL_FIELDS_IN_SIDECAR",
                    "reversible": True,
                    "detail": "integer positions and permille confidence are retained in v1-origin sidecar",
                },
            ]
        )
    elif state.source_version == "v2" and state.target_version == "v1":
        if not irreversible_losses:
            conversion_changes.append(
                {
                    "code": "V2_TO_V1_EXACT_QUANTIZATION",
                    "reversible": True,
                    "detail": "all positions and confidence values quantized without information loss",
                }
            )
    diagnostic_failures = [
        Failure.from_dict(item)
        for item in (
            v1_roundtrip["parse_failures"]
            + v1_roundtrip["serialization_failures"]
            + v2_roundtrip["parse_failures"]
            + v2_roundtrip["serialization_failures"]
        )
    ]
    all_failures = sorted(state.failures + diagnostic_failures, key=Failure.sort_key)
    warning_codes = [str(item.get("business_code")) for item in state.warnings]
    injections = match_injections(injection_config or {"injections": []}, all_failures)
    report = {
        "schema": "domain-diagnostics/1",
        "source_version": state.source_version,
        "source_fingerprint": state.source_fingerprint,
        "target_version": state.target_version,
        "ir_fingerprint": state.ir_fingerprint,
        "loss_hash": state.loss_hash,
        "degradation_fingerprint": state.degradation_fingerprint,
        "entry_counts": state.entry_counts(),
        "roundtrips": {"v1": v1_roundtrip, "v2": v2_roundtrip},
        "degradation": {
            "target_version": state.target_version,
            "reversible_losses": reversible_losses,
            "irreversible_losses": irreversible_losses,
            "conversion_changes": conversion_changes,
            "reversible_count": len(reversible_losses) + len(conversion_changes),
            "irreversible_count": len(irreversible_losses),
        },
        "business_code_summary": _code_summary(all_failures, warning_codes),
        "failures": [failure.to_dict() for failure in state.failures],
        "diagnostic_failures": [failure.to_dict() for failure in diagnostic_failures],
        "warnings": state.warnings,
        "injections_hit": injections,
    }
    return report


def report_bytes(report: dict[str, Any]) -> bytes:
    return dumps_canonical(report, newline=True).encode("utf-8")
