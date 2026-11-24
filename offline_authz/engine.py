"""Deterministic multi-factor authorization engine."""

from __future__ import annotations

from typing import Any

from .codes import (
    BLOCKING_CODES,
    E_CREDENTIAL_NOT_FOUND,
    E_DEVICE_BINDING_REVOKED,
    E_DEVICE_MISMATCH,
    E_DEVICE_NOT_BOUND,
    E_DEVICE_NOT_FOUND,
    E_DIGEST_TIER_TOO_LOW,
    E_FACTOR_NOT_BOUND,
    E_FACTOR_PROOF_INVALID,
    E_FACTOR_REVOKED,
    E_FACTOR_TYPE_MISMATCH,
    E_FINGERPRINT_AMBIGUOUS,
    E_INVALIDATED_BY_CLOSURE,
    E_ITERATIONS_TOO_LOW,
    E_MISSING_FACTOR,
    E_POLICY_CONFLICT,
    E_POLICY_EXPIRED,
    E_POLICY_NOT_EFFECTIVE,
    E_POLICY_NOT_FOUND,
    E_POLICY_TOMBSTONED,
    E_BIND_CYCLE,
)
from .factors import verify_submitted_proof
from .fingerprint import compare_canonical_fingerprints
from .graph import (
    attempt_dependency_nodes,
    build_dependency_graph,
    find_cycles,
    reachable_closure,
    revocation_roots,
)
from .jsonio import ordered_map, sha256_text
from .policy import canonical_policy_key, select_device_binding_at, select_policy_at


def adjudicate_bundle(
    bundle: Any,
    view: dict[str, Any],
    workers: int = 1,
    attempts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    users = {record["user_id"]: record for record in bundle.users}
    devices = {record["device_id"]: record for record in bundle.devices}
    factors = {record["factor_id"]: record for record in bundle.factors}
    graph = build_dependency_graph(view_bindings(bundle, view))
    cycle_index = find_cycles(graph)
    context = {
        "bundle": bundle,
        "view": view,
        "users": users,
        "devices": devices,
        "factors": factors,
        "graph": graph,
        "cycles": cycle_index,
    }
    source_attempts = bundle.valid_attempts if attempts is None else attempts
    ordered_attempts = sorted(source_attempts, key=lambda a: (a["user_id"], a["attempt_seq"]))
    return ordered_map(ordered_attempts, lambda attempt: evaluate_attempt(attempt, context), workers)


def view_bindings(bundle: Any, view: dict[str, Any]) -> Any:
    if not hasattr(bundle, "_bindings"):
        bundle._bindings = view.get("bindings", [])
    return bundle


def evaluate_attempt(attempt: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    bundle = context["bundle"]
    view = context["view"]
    users = context["users"]
    devices = context["devices"]
    factors = context["factors"]
    at_ms = attempt["observed_at_ms"]
    codes: list[str] = []
    warnings: list[dict[str, Any]] = []
    details: dict[str, Any] = {}

    raw_policy_key = attempt.get("policy_key") or "mfa-default"
    policy_key = canonical_policy_key(raw_policy_key)
    group = view.get("policy_groups", {}).get(policy_key)
    policy, policy_state = select_policy_at(view, policy_key, at_ms)
    policy_generation = policy["generation"] if policy else None

    if group and group.get("conflict"):
        details["policy_conflict"] = group["conflict"]
        if not group.get("authority_resolved", True):
            codes.append(E_POLICY_CONFLICT)
        else:
            warnings.append(
                {
                    "code": E_POLICY_CONFLICT,
                    "severity": "RESOLVED_AUTHORITY_CONFLICT",
                    "authoritative_source": group["authoritative"]["source"],
                }
            )

    if policy_state in {"missing", "not_effective"} or policy is None:
        code = {
            "missing": E_POLICY_NOT_FOUND,
            "not_effective": E_POLICY_NOT_EFFECTIVE,
        }.get(policy_state or "missing", E_POLICY_NOT_FOUND)
        codes.append(code)
    elif policy_state in {"expired", "tombstoned"}:
        codes.append(E_POLICY_EXPIRED if policy_state == "expired" else E_POLICY_TOMBSTONED)
        details["policy"] = {
            "policy_key": policy_key,
            "generation": policy["generation"],
            "source": policy["source"],
            "state": policy_state,
        }
    else:
        details["policy"] = {
            "policy_key": policy_key,
            "generation": policy["generation"],
            "source": policy["source"],
            "expires_at_ms": policy["expires_at_ms"],
        }

    roots = revocation_roots(bundle, view=view, at_ms=at_ms)
    closure = reachable_closure(context["graph"], [root["node"] for root in roots])
    dependencies = attempt_dependency_nodes(attempt, bundle)
    closure_hits = sorted(node for node in dependencies if node in closure)
    if closure_hits:
        codes.append(E_INVALIDATED_BY_CLOSURE)
        details["closure_hits"] = closure_hits
    root_nodes = {root["node"]: root for root in roots}
    for node in dependencies:
        root = root_nodes.get(node)
        if node.startswith("factor:") and root and root.get("reason") == "factor_unbound":
            codes.append(E_FACTOR_REVOKED)
    cycle_hits = [
        cycle
        for cycle in context["cycles"]
        if any(node in set(cycle["nodes"]) for node in dependencies)
    ]
    if cycle_hits:
        codes.append(E_BIND_CYCLE)
        details["bind_cycles"] = cycle_hits

    canonical_fp_key = None
    bound_fp_key = None
    if attempt.get("device_id"):
        device = devices.get(attempt["device_id"])
        if device is None:
            codes.append(E_DEVICE_NOT_FOUND)
        elif device["user_id"] != attempt["user_id"]:
            codes.append(E_DEVICE_MISMATCH)
            canonical_fp_key = device.get("canonical_fingerprint_key")
            details["device_user_id"] = device["user_id"]
        else:
            fingerprint = device["canonical_fingerprint"]
            canonical_fp_key = fingerprint["canonical_fingerprint_key"]
            binding = select_device_binding_at(view, policy_key, attempt["user_id"], device["device_id"], at_ms)
            if binding is None:
                codes.append(E_DEVICE_NOT_BOUND)
            elif binding.get("tombstoned"):
                bound_fp_key = binding.get("canonical_fingerprint_key")
                codes.append(E_DEVICE_BINDING_REVOKED)
                details["binding_generation"] = binding["generation"]
            else:
                bound_fp_key = binding["canonical_fingerprint_key"]
                right = {
                    "canonical_fingerprint_key": bound_fp_key,
                    "points": binding.get("canonical_vector") or [],
                    "canonical_decimals": fingerprint["canonical_decimals"],
                }
                comparison = compare_canonical_fingerprints(
                    fingerprint, right, bundle.fingerprint_threshold
                )
                details["fingerprint_comparison"] = comparison
                details["binding_generation"] = binding["generation"]
                if comparison["comparison"] == "AMBIGUOUS":
                    codes.append(E_FINGERPRINT_AMBIGUOUS)
                elif comparison["comparison"] == "MISMATCH":
                    codes.append(E_DEVICE_MISMATCH)
    else:
        codes.append(E_DEVICE_NOT_FOUND)

    factor_results = _evaluate_factors(attempt, context, policy, codes, warnings)

    credential = users.get(attempt["user_id"])
    credential_check: dict[str, Any] | None = None
    if credential is None:
        codes.append(E_CREDENTIAL_NOT_FOUND)
    elif policy is not None:
        tier_ok = credential["digest_tier"] >= policy["min_digest_tier"]
        iterations_ok = credential["iterations"] >= policy["min_iterations"]
        credential_check = {
            "actual_digest_tier": credential["digest_tier"],
            "required_digest_tier": policy["min_digest_tier"],
            "tier_satisfied": tier_ok,
            "actual_iterations": credential["iterations"],
            "required_iterations": policy["min_iterations"],
            "iterations_satisfied": iterations_ok,
        }
        if not tier_ok:
            codes.append(E_DIGEST_TIER_TOO_LOW)
        if not iterations_ok:
            codes.append(E_ITERATIONS_TOO_LOW)

    unique_codes = sorted(set(codes))
    decision = "ALLOW" if not any(code in BLOCKING_CODES for code in unique_codes) else "DENY"
    stable_result = {
        "user_id": attempt["user_id"],
        "attempt_seq": attempt["attempt_seq"],
        "batch_watermark": attempt["batch_watermark"],
        "logical_clock": attempt["logical_clock"],
        "observed_at_ms": at_ms,
        "decision": decision,
        "codes": unique_codes,
        "warnings": warnings,
        "policy_key": policy_key,
        "policy_generation": policy_generation,
        "canonical_fingerprint_key": canonical_fp_key,
        "bound_fingerprint_key": bound_fp_key,
        "binding_generation": details.get("binding_generation"),
        "fingerprint_error_bound": (
            device["canonical_fingerprint"].get("error_bound_decimal")
            if attempt.get("device_id") and devices.get(attempt["device_id"])
            else None
        ),
        "fingerprint_comparison": {
            **(details.get("fingerprint_comparison") or {}),
            "basis_generation": details.get("binding_generation"),
        } if details.get("fingerprint_comparison") else None,
        "factor_results": factor_results,
        "credential_check": credential_check,
        "closure_hits": details.get("closure_hits", []),
        "bind_cycles": details.get("bind_cycles", []),
        "policy_conflict": details.get("policy_conflict"),
        "judgement_watermark": attempt["logical_clock"],
        "session_id": attempt.get("session_id"),
    }
    stable_result["input_hash"] = sha256_text(
        {"snapshot_version": bundle.snapshot_version, "attempt": attempt}
    )
    stable_result["result_hash"] = sha256_text(_ledger_digest_object(stable_result))
    return stable_result


def select_policy_at_include_expired(view: dict[str, Any], key: str, at_ms: int):
    group = view.get("policy_groups", {}).get(key)
    if not group:
        return None, None
    effective = [r for r in group.get("revisions", []) if not r["tombstoned"] and r["effective_at_ms"] <= at_ms]
    if not effective:
        return None, None
    return max(effective, key=lambda r: (r["generation"], r["effective_at_ms"])), "expired"


def _evaluate_factors(
    attempt: dict[str, Any],
    context: dict[str, Any],
    policy: dict[str, Any] | None,
    codes: list[str],
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    factors_by_id = context["factors"]
    required = set(policy["required_factors"]) if policy else set()
    submitted = attempt.get("factors", [])
    results: list[dict[str, Any]] = []
    satisfied_types: set[str] = set()
    outside_policy_results: list[dict[str, Any]] = []

    for item in submitted:
        if not isinstance(item, dict):
            codes.append(E_FACTOR_NOT_BOUND)
            results.append({"valid": False, "code": E_FACTOR_NOT_BOUND})
            continue
        factor_id = item.get("factor_id")
        factor = factors_by_id.get(factor_id) if isinstance(factor_id, str) else None
        item_result: dict[str, Any] = {
            "factor_id": factor_id,
            "submitted_type": item.get("factor_type"),
            "valid": False,
        }
        if factor is None or factor.get("user_id") != attempt["user_id"]:
            codes.append(E_FACTOR_NOT_BOUND)
            item_result["code"] = E_FACTOR_NOT_BOUND
            results.append(item_result)
            continue
        item_result["factor_type"] = factor["factor_type"]
        if factor.get("status") != "BOUND":
            codes.append(E_FACTOR_REVOKED)
            item_result["code"] = E_FACTOR_REVOKED
            results.append(item_result)
            continue
        submitted_type = item.get("factor_type")
        if submitted_type not in {"TOTP", "WEBAUTHN", "PUSH"} or submitted_type != factor["factor_type"]:
            codes.append(E_FACTOR_TYPE_MISMATCH)
            item_result["code"] = E_FACTOR_TYPE_MISMATCH
            item_result["expected_type"] = factor["factor_type"]
            results.append(item_result)
            continue
        has_proof_material = any(
            key in item
            for key in ("proof_digest", "challenge", "code", "client_data_digest", "push_response")
        )
        if has_proof_material:
            proof_ok, proof_reason = verify_submitted_proof(factor, item)
            item_result["proof_ok"] = proof_ok
            if not proof_ok:
                codes.append(E_FACTOR_PROOF_INVALID)
                item_result["code"] = E_FACTOR_PROOF_INVALID
                item_result["reason"] = proof_reason
                results.append(item_result)
                continue
        else:
            item_result["proof_ok"] = None
            item_result["verification"] = "BOUND_FACTOR_REFERENCE_ONLY"
        item_result["valid"] = True
        item_result["code"] = "OK_FACTOR"
        if required and submitted_type not in required:
            item_result["policy_type_match"] = False
            item_result["required_types"] = sorted(required)
            item_result["code"] = "W_FACTOR_TYPE_NOT_REQUIRED"
            outside_policy_results.append(item_result)
            warnings.append(
                {
                    "code": "W_FACTOR_TYPE_NOT_REQUIRED",
                    "factor_id": factor_id,
                    "submitted_type": submitted_type,
                }
            )
        else:
            satisfied_types.add(submitted_type)
        results.append(item_result)

    missing_types: list[str] = []
    for factor_type in sorted(required):
        if factor_type not in satisfied_types:
            missing_types.append(factor_type)
            results.append({"factor_type": factor_type, "valid": False, "code": E_MISSING_FACTOR})
    if missing_types:
        codes.append(E_MISSING_FACTOR)
    if missing_types and outside_policy_results:
        codes.append(E_FACTOR_TYPE_MISMATCH)
        for item_result in outside_policy_results:
            item_result["code"] = E_FACTOR_TYPE_MISMATCH
    return results


def _ledger_digest_object(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": result["user_id"],
        "attempt_seq": result["attempt_seq"],
        "decision": result["decision"],
        "codes": result["codes"],
        "policy_key": result["policy_key"],
        "policy_generation": result["policy_generation"],
        "canonical_fingerprint_key": result["canonical_fingerprint_key"],
        "judgement_watermark": result["judgement_watermark"],
    }
