from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .canonical import canonical_bytes, sha256_bytes, sha256_file
from .errors import PipelineError, error
from .manifest import TaskSpec


KERNEL_VERSION = "fixed-kernel-v1"
RESULT_SCHEMA = "pipeline-result-v1"
AMOUNT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _parse_amount_units(amount: Any, scale: int) -> int:
    if not isinstance(amount, str) or not AMOUNT_RE.fullmatch(amount):
        raise error(
            "E_INPUT_INVALID",
            "amount must be a canonical decimal string",
            amount=amount,
        )
    negative = amount.startswith("-")
    unsigned = amount[1:] if negative else amount
    whole_text, _, fraction_text = unsigned.partition(".")
    if len(fraction_text) > scale:
        raise error(
            "E_INPUT_INVALID",
            "amount has more fractional digits than fixed_scale",
            amount=amount,
            fixed_scale=scale,
        )
    units = int(whole_text) * (10**scale)
    if scale:
        units += int((fraction_text + "0" * scale)[:scale])
    return -units if negative else units


def _format_units(units: int, scale: int) -> str:
    if scale == 0:
        return str(units)
    sign = "-" if units < 0 else ""
    absolute = abs(units)
    factor = 10**scale
    return f"{sign}{absolute // factor}.{absolute % factor:0{scale}d}"


def _round_half_away(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise error("E_INPUT_INVALID", "weighted denominator is zero")
    if denominator < 0:
        numerator = -numerator
        denominator = -denominator
    sign = -1 if numerator < 0 else 1
    absolute = abs(numerator)
    rounded = (absolute * 2 + denominator) // (2 * denominator)
    return sign * rounded


def _require_plain_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("E_INPUT_INVALID", f"{field} must be an integer", field=field, value=value)
    return value


def _load_json_file(path: Path, label: str) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise error("E_INPUT_MISSING", "input file does not exist", input=label)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error(
            "E_INPUT_INVALID",
            "input JSON cannot be parsed",
            input=label,
            reason=str(exc),
        )


def _records_from_document(document: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict) and isinstance(document.get("records"), list):
        records = document["records"]
    else:
        raise error(
            "E_INPUT_INVALID",
            "input must be a record list or an object with a records list",
            input=label,
        )
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise error(
                "E_INPUT_INVALID",
                "each record must be an object",
                input=label,
                record_index=index,
            )
    return records


def _normalise_record(record: dict[str, Any], scale: int, label: str, index: int) -> dict[str, Any]:
    key = record.get("key")
    if not isinstance(key, str) or not key:
        raise error(
            "E_INPUT_INVALID",
            "record key must be a non-empty string",
            input=label,
            record_index=index,
        )
    value = _require_plain_int(record.get("value"), "value")
    weight = _require_plain_int(record.get("weight"), "weight")
    if weight <= 0:
        raise error(
            "E_INPUT_INVALID",
            "record weight must be positive",
            input=label,
            record_index=index,
        )
    amount = record.get("amount")
    units = _parse_amount_units(amount, scale)
    return {
        "key": key,
        "value": value,
        "weight": weight,
        "amount": _format_units(units, scale),
        "amount_units": units,
    }


def _source_label(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def serial_aggregate(
    task: TaskSpec,
    input_paths: Iterable[Path],
    root: Path,
    *,
    fixed_scale: int,
    environment: dict[str, str] | None = None,
    kernel_version: str = KERNEL_VERSION,
) -> dict[str, Any]:
    all_records: list[dict[str, Any]] = []
    input_digests: list[dict[str, Any]] = []
    for path in input_paths:
        label = _source_label(path, root)
        document = _load_json_file(path, label)
        records = _records_from_document(document, label)
        input_digests.append(
            {
                "path": label,
                "records": len(records),
                "sha256": sha256_file(path),
            }
        )
        for index, record in enumerate(records):
            normalised = _normalise_record(record, fixed_scale, label, index)
            normalised["_source"] = label
            normalised["_source_index"] = index
            all_records.append(normalised)

    integer_sum = 0
    fixed_sum_units = 0
    weighted_numerator = 0
    weighted_denominator = 0
    groups: dict[str, dict[str, Any]] = {}
    ordered_steps: list[dict[str, Any]] = []
    for index, record in enumerate(all_records):
        key = record["key"]
        amount_units = record["amount_units"]
        weight = record["weight"]
        integer_sum += record["value"]
        fixed_sum_units += amount_units
        weighted_numerator += amount_units * weight
        weighted_denominator += weight

        ordered_steps.append(
            {
                "index": index,
                "integer_sum": integer_sum,
                "fixed_sum_units": fixed_sum_units,
                "weighted_numerator": weighted_numerator,
                "weighted_denominator": weighted_denominator,
                "key": key,
                "source": record["_source"],
                "source_index": record["_source_index"],
            }
        )

        group = groups.get(key)
        if group is None:
            group = {
                "key": key,
                "record_count": 0,
                "integer_sum": 0,
                "fixed_sum_units": 0,
                "weighted_numerator": 0,
                "weighted_denominator": 0,
            }
            groups[key] = group
        group["record_count"] += 1
        group["integer_sum"] += record["value"]
        group["fixed_sum_units"] += amount_units
        group["weighted_numerator"] += amount_units * weight
        group["weighted_denominator"] += weight

    for group in groups.values():
        denominator = group["weighted_denominator"]
        units = _round_half_away(group["weighted_numerator"], denominator)
        group["weighted_defined"] = denominator != 0
        group["weighted_fixed_units"] = units
        group["weighted_fixed"] = _format_units(units, fixed_scale)
        group["fixed_sum"] = _format_units(group["fixed_sum_units"], fixed_scale)

    total_weighted_units = _round_half_away(weighted_numerator, weighted_denominator)
    environment_json = {
        key: (environment or {})[key] for key in sorted((environment or {}).keys())
    }
    records_output = [
        {
            "key": record["key"],
            "value": record["value"],
            "weight": record["weight"],
            "amount": record["amount"],
        }
        for record in all_records
    ]
    return {
        "schema": RESULT_SCHEMA,
        "task": task.name,
        "kernel_version": kernel_version,
        "dependencies": list(task.depends_on),
        "environment": environment_json,
        "fixed_scale": fixed_scale,
        "records_processed": len(records_output),
        "records": records_output,
        "integer_sum": integer_sum,
        "fixed_sum_units": fixed_sum_units,
        "fixed_sum": _format_units(fixed_sum_units, fixed_scale),
        "weighted_numerator": weighted_numerator,
        "weighted_denominator": weighted_denominator,
        "weighted_defined": weighted_denominator != 0,
        "weighted_fixed_units": total_weighted_units,
        "weighted_fixed": _format_units(total_weighted_units, fixed_scale),
        "groups": [groups[key] for key in sorted(groups)],
        "intermediates": {
            "fixed_scale_factor": 10**fixed_scale,
            "ordered_steps": ordered_steps,
        },
        "input_digests": input_digests,
    }


def result_bytes(result: dict[str, Any]) -> bytes:
    return canonical_bytes(result)


def result_digest(result: dict[str, Any]) -> str:
    return sha256_bytes(result_bytes(result))


def compare_results(
    serial_result: dict[str, Any],
    sandbox_result: dict[str, Any],
    *,
    task: str,
    dependencies: Iterable[str],
) -> None:
    serial_data = result_bytes(serial_result)
    sandbox_data = result_bytes(sandbox_result)
    if serial_data == sandbox_data:
        return
    from .canonical import first_difference

    difference = first_difference(serial_result, sandbox_result)
    raise error(
        "E_EXECUTION_DIVERGENCE",
        "serial and sandbox execution results differ",
        task=task,
        first_divergent_field=(difference or {}).get("field"),
        serial_value=(difference or {}).get("serial"),
        sandbox_value=(difference or {}).get("sandbox"),
        serial_intermediates=serial_result.get("intermediates"),
        sandbox_intermediates=sandbox_result.get("intermediates"),
        dependency_sources={
            "depends_on": list(dependencies),
            "inputs": [
                item.get("path") for item in serial_result.get("input_digests", [])
            ],
        },
    )


def read_records_for_probe(path: Path) -> Any:
    """Small helper kept public for child-side test probes."""
    return _load_json_file(path, PurePosixPath(path).as_posix())
