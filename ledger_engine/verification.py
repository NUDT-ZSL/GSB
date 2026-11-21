from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

from .engine import _metric_payload, compare_sensitive_pairs, compute_truth
from .jsonutil import (
    canonical_decimal,
    fraction_from,
    hash_json,
    parse_fraction_string,
)
from .model import DERIVED_ORDER, Snapshot
from .numeric import (
    FixedValue,
    floor_fraction,
    fraction_from_entry_string,
    recompute_entry,
)
from .persistence import (
    committed_services,
    current_state,
    ledger_entries,
    pending_round,
    round_info,
    round_result_hash,
    sensitive_pairs,
    service_result,
    snapshot_for_version,
)


def verify_database(connection: Any) -> dict[str, Any]:
    state = current_state(connection)
    errors: list[dict[str, Any]] = []
    if state["round_id"] is None:
        return {
            "checks": [],
            "errors": [
                {
                    "code": "E_NO_RESULT",
                    "message": "there is no committed diagnostic result",
                }
            ],
            "ok": False,
            "schema": "infra-ledger-verify/v1",
        }

    round_id = state["round_id"]
    info = round_info(connection, round_id)
    snapshot = snapshot_for_version(connection, info["version"])
    truth = compute_truth(snapshot)
    expected_services = snapshot.topological_order()
    committed = committed_services(connection, round_id)
    if committed != sorted(expected_services):
        errors.append(
            _error(
                "E_SERVICE_SET",
                "committed services do not match the topology",
                {"committed": committed, "expected": sorted(expected_services)},
            )
        )

    payloads: dict[str, dict[str, Any]] = {}
    ledger_row_count = 0
    for service in expected_services:
        result = service_result(connection, round_id, service)
        if result is None:
            errors.append(
                _error(
                    "E_MISSING_SERVICE",
                    "service result is missing",
                    {"service": service},
                )
            )
            continue
        payload = result["payload"]
        payloads[service] = payload
        if result["payload_hash"] != hash_json(payload):
            errors.append(
                _error(
                    "E_PAYLOAD_HASH",
                    "service payload hash does not match its contents",
                    {"service": service},
                )
            )
        if payload.get("service") != service:
            errors.append(
                _error(
                    "E_PAYLOAD_SERVICE",
                    "service payload identifies the wrong service",
                    {"service": service},
                )
            )
        entries = ledger_entries(connection, round_id, service)
        ledger_row_count += len(entries)
        entry_map: dict[int, dict[str, Any]] = {}
        key_map: dict[str, dict[str, Any]] = {}
        for row in entries:
            if row["status"] != "COMMITTED":
                errors.append(
                    _error(
                        "E_UNCOMMITTED_ENTRY",
                        "committed service has an uncommitted ledger entry",
                        {
                            "registration_no": row["registration_no"],
                            "service": service,
                        },
                    )
                )
            entry = row["entry"]
            if row["entry_hash"] != hash_json(entry):
                errors.append(
                    _error(
                        "E_ENTRY_HASH",
                        "ledger entry hash does not match its contents",
                        {
                            "registration_no": row["registration_no"],
                            "service": service,
                        },
                    )
                )
            if entry.get("registration_no") != row["registration_no"]:
                errors.append(
                    _error(
                        "E_ENTRY_NUMBER",
                        "entry registration number differs from its row",
                        {
                            "registration_no": row["registration_no"],
                            "service": service,
                        },
                    )
                )
            if entry.get("registration_key") != row["registration_key"]:
                errors.append(
                    _error(
                        "E_ENTRY_KEY",
                        "entry registration key differs from its row",
                        {
                            "registration_no": row["registration_no"],
                            "service": service,
                        },
                    )
                )
            entry_map[int(row["registration_no"])] = entry
            key_map[str(row["registration_key"])] = entry
            _verify_entry(entry, errors)

        derived_payload = payload.get("derived", {})
        for metric in DERIVED_ORDER:
            stored = derived_payload.get(metric)
            if stored is None:
                errors.append(
                    _error(
                        "E_MISSING_DERIVED",
                        "service result is missing a derived metric",
                        {"derived": metric, "service": service},
                    )
                )
                continue
            _verify_metric(
                snapshot=snapshot,
                service=service,
                metric=metric,
                stored=stored,
                truth=truth[service][metric],
                errors=errors,
            )
            for source in stored.get("sources", []):
                _verify_source(
                    source=source,
                    service=service,
                    entry_map=entry_map if source.get("service") == service else None,
                    errors=errors,
                )

        referenced_keys = {
            source["registration_key"]
            for metric in derived_payload.values()
            for source in metric.get("sources", [])
            if source.get("service") == service
        }
        unreferenced = sorted(set(key_map) - referenced_keys)
        if unreferenced:
            errors.append(
                _error(
                    "E_UNREFERENCED_ENTRY",
                    "ledger contains entries not referenced by a derived chain",
                    {"keys": unreferenced, "service": service},
                )
            )

    stored_pairs = sensitive_pairs(connection, round_id)
    recomputed_pairs = compare_sensitive_pairs(snapshot, payloads)
    if [hash_json(pair) for pair in stored_pairs] != [
        hash_json(pair) for pair in recomputed_pairs
    ]:
        errors.append(
            _error(
                "E_SENSITIVE_MISMATCH",
                "sensitive comparison records do not match recomputation",
                {
                    "stored": len(stored_pairs),
                    "recomputed": len(recomputed_pairs),
                },
            )
        )

    invalidations = info["invalidations"]
    service_hashes = {
        service: service_result(connection, round_id, service)["payload_hash"]
        for service in expected_services
        if service_result(connection, round_id, service) is not None
    }
    pair_hashes = {
        pair["pair_key"]: hash_json(pair)
        for pair in stored_pairs
    }
    recomputed_result_hash = round_result_hash(
        service_hashes=service_hashes,
        pair_hashes=pair_hashes,
        invalidations=invalidations,
    )
    if info["result_hash"] != recomputed_result_hash:
        errors.append(
            _error(
                "E_RESULT_HASH",
                "round result hash does not match persisted records",
                {
                    "stored": info["result_hash"],
                    "recomputed": recomputed_result_hash,
                },
            )
        )
    if info["version"] != state["version"]:
        errors.append(
            _error(
                "E_CURRENT_POINTER",
                "current state version differs from the committed round",
            )
        )

    pending = pending_round(connection)
    checks = [
        "CONFIG_VERSION",
        "DECIMAL_TRUTH",
        "ERROR_INTERVALS",
        "LEDGER_ARITHMETIC",
        "REGISTRATION_KEYS",
        "SENSITIVE_REDERIVATION",
        "RESULT_HASH",
    ]
    return {
        "checks": checks,
        "errors": sorted(errors, key=lambda item: (item["code"], item.get("details", {}).get("service", ""))),
        "ledger_entries": ledger_row_count,
        "logical_clock": info["clock"],
        "ok": not errors,
        "pending_round": pending,
        "result_hash": info["result_hash"],
        "round_id": round_id,
        "schema": "infra-ledger-verify/v1",
        "sensitive_pairs": len(stored_pairs),
        "service_count": len(payloads),
        "version": info["version"],
    }


def _verify_metric(
    *,
    snapshot: Snapshot,
    service: str,
    metric: str,
    stored: dict[str, Any],
    truth: Decimal,
    errors: list[dict[str, Any]],
) -> None:
    config = snapshot.config
    try:
        units = int(stored["fixed_units"])
        scale = int(stored["fixed_scale"])
        low = parse_fraction_string(stored["error_interval"]["low"])
        high = parse_fraction_string(stored["error_interval"]["high"])
    except Exception:
        errors.append(
            _error(
                "E_METRIC_SHAPE",
                "derived metric is missing its numeric ledger fields",
                {"derived": metric, "service": service},
            )
        )
        return
    expected_truth = canonical_decimal(truth)
    if stored.get("truth") != expected_truth:
        errors.append(
            _error(
                "E_TRUTH_MISMATCH",
                "stored decimal truth differs from full recomputation",
                {"derived": metric, "service": service},
            )
        )
    fixed = FixedValue(units=units, scale=scale, low=low, high=high)
    epsilon = Fraction(1, 10 ** max(1, config.truth_precision - 5))
    truth_fraction = Fraction(truth)
    if not (low - epsilon <= truth_fraction <= high + epsilon):
        errors.append(
            _error(
                "E_TRUTH_INTERVAL",
                "truth value is outside the stored error interval",
                {"derived": metric, "service": service},
            )
        )
    recomputed = _metric_payload(
        fixed_value=fixed,
        truth_value=truth,
        sources=stored.get("sources", []),
        config=config,
    )
    for field in (
        "abs_deviation",
        "error_bound",
        "error_interval",
        "first_defect",
        "fixed",
        "fixed_scale",
        "fixed_units",
        "ledger_ok",
        "rel_deviation",
        "sources",
        "truth",
    ):
        if stored.get(field) != recomputed.get(field):
            errors.append(
                _error(
                    "E_METRIC_FIELD",
                    "derived metric field differs from recomputation",
                    {
                        "derived": metric,
                        "field": field,
                        "service": service,
                    },
                )
            )


def _verify_source(
    *,
    source: dict[str, Any],
    service: str,
    entry_map: dict[int, dict[str, Any]] | None,
    errors: list[dict[str, Any]],
) -> None:
    if entry_map is None:
        return
    number = int(source["registration_no"])
    entry = entry_map.get(number)
    if entry is None:
        errors.append(
            _error(
                "E_SOURCE_MISSING",
                "derived chain references a missing registration",
                {"registration_no": number, "service": service},
            )
        )
        return
    if entry != source:
        errors.append(
            _error(
                "E_SOURCE_MISMATCH",
                "derived chain entry differs from the persisted registration",
                {"registration_no": number, "service": service},
            )
        )


def _verify_entry(entry: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    service = entry.get("service")
    number = entry.get("registration_no")
    operation = entry.get("operation")
    try:
        if operation == "CONVERT":
            value = fraction_from(entry["input"], field="entry.input")
            carry = fraction_from_entry_string(entry["carry_in"])
            scale = int(entry["output_scale"])
            raw = value * (10**scale) + carry
            units = floor_fraction(raw)
            remainder = raw - units
            expected_direction = "EXACT" if remainder == 0 else "FLOOR"
            if units != int(entry["output_units"]):
                raise ValueError("conversion units")
            if remainder != fraction_from_entry_string(entry["discarded_units"]):
                raise ValueError("conversion remainder")
            if remainder != fraction_from_entry_string(entry["carry_out"]):
                raise ValueError("conversion carry")
            if entry["direction"] != expected_direction:
                raise ValueError("conversion direction")
        else:
            units, scale, remainder = recompute_entry(entry)
            if units != int(entry["output_units"]):
                raise ValueError("arithmetic units")
            if remainder != fraction_from_entry_string(entry["discarded_units"]):
                raise ValueError("arithmetic remainder")
            expected_direction = "EXACT" if remainder == 0 else "FLOOR"
            if entry["direction"] != expected_direction:
                raise ValueError("arithmetic direction")
    except Exception as exc:
        errors.append(
            _error(
                "E_LEDGER_ARITHMETIC",
                "ledger registration cannot be reproduced",
                {
                    "operation": operation,
                    "registration_no": number,
                    "service": service,
                    "reason": str(exc),
                },
            )
        )


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "details": details or {},
        "message": message,
    }
