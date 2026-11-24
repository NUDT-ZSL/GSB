from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from .codes import (
    E_ALGORITHM_NOT_ALLOWED,
    E_MIGRATION_REQUIRED,
    E_ITERATIONS_TOO_LOW,
    E_OLD_TIER_AFTER_MIGRATION,
    E_POLICY_CONFLICT,
    E_SIGNATURE_MISMATCH,
    E_TOKEN_EXPIRED,
    E_TOKEN_NOT_FOUND,
    E_TOKEN_NOT_YET_VALID,
    E_TOKEN_REUSED,
    E_NOT_ONE_TIME,
    E_USER_NOT_FOUND,
    OK,
)
from .models import ALGORITHM_CATALOG


def group_credentials(credentials: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for credential in credentials:
        grouped[credential["user_id"]].append(credential)
    for items in grouped.values():
        items.sort(key=lambda item: (item["generation"], item.get("effective_at_ms") or 0, item["record_index"]))
    return grouped


def choose_current_credential(credentials: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not credentials:
        raise ValueError("at least one credential is required")
    return max(
        credentials,
        key=lambda item: (item["generation"], item.get("effective_at_ms") or 0, item["record_index"]),
    )


def evaluate_user(
    user_id: str,
    credentials_by_user: Dict[str, List[Dict[str, Any]]],
    policy_view: Any,
    *,
    at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    at_ms = policy_view.observed_clock_ms if at_ms is None else at_ms
    credentials = credentials_by_user.get(user_id, [])
    if not credentials:
        return {
            "user_id": user_id,
            "status": "ERROR",
            "code": E_USER_NOT_FOUND,
            "message": "no credential records for user",
            "basis": None,
        }

    current = choose_current_credential(credentials)
    policy = policy_view.resolve(user_id, at_ms)
    if policy["status"] == "CONFLICT":
        return {
            "user_id": user_id,
            "status": "POLICY_CONFLICT",
            "code": E_POLICY_CONFLICT,
            "message": "conflicting policy conclusions",
            "current_credential": _credential_ref(current),
            "credential_generation": current["generation"],
            "basis_policy_key": policy.get("policy_key"),
            "basis_policy_generation": None,
            "basis": policy.get("basis"),
            "reason_codes": [E_POLICY_CONFLICT],
        }
    if policy["status"] != "OK":
        return {
            "user_id": user_id,
            "status": "ERROR",
            "code": policy.get("code", "E_NO_POLICY"),
            "message": "no authoritative policy",
            "current_credential": _credential_ref(current),
            "credential_generation": current["generation"],
            "basis_policy_key": policy.get("policy_key"),
            "basis_policy_generation": None,
            "basis": policy,
            "reason_codes": [policy.get("code", "E_NO_POLICY")],
        }

    reasons: List[str] = []
    if current["algorithm"] not in policy["allowed_algorithms"]:
        reasons.append(E_ALGORITHM_NOT_ALLOWED)
    if current["iterations"] < policy["min_iterations"]:
        reasons.append(E_ITERATIONS_TOO_LOW)

    algorithm_info = ALGORITHM_CATALOG[current["algorithm"]]
    if reasons:
        status = "PENDING_MIGRATION"
        code = E_MIGRATION_REQUIRED
    else:
        status = "UP_TO_DATE"
        code = OK

    return {
        "user_id": user_id,
        "status": status,
        "code": code,
        "reason_codes": reasons,
        "current_algorithm_is_new_tier": bool(algorithm_info.get("new_tier")),
        "current_credential": _credential_ref(current),
        "credential_generation": current["generation"],
        "current_algorithm": current["algorithm"],
        "current_iterations": current["iterations"],
        "basis_policy_key": policy["policy_key"],
        "basis_policy_generation": policy["generation"],
        "basis_effective_at_ms": policy["effective_at_ms"],
        "required_allowed_algorithms": list(policy["allowed_algorithms"]),
        "required_min_iterations": policy["min_iterations"],
        "basis": policy.get("basis"),
        "direct_tombstone": policy.get("direct_tombstone", False),
    }


def _credential_ref(credential: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "record_id": credential["record_id"],
        "generation": credential["generation"],
        "algorithm": credential["algorithm"],
        "iterations": credential["iterations"],
        "effective_at_ms": credential.get("effective_at_ms"),
        "source_category": credential.get("source_category"),
        "record_index": credential.get("record_index"),
    }


def evaluate_all_users(
    user_ids: Iterable[str],
    credentials_by_user: Dict[str, List[Dict[str, Any]]],
    policy_view: Any,
) -> List[Dict[str, Any]]:
    return [
        evaluate_user(user_id, credentials_by_user, policy_view)
        for user_id in sorted(user_ids)
    ]


def _find_signature_source(
    signature: str,
    credentials: List[Dict[str, Any]],
    signing_algorithm: Optional[str],
) -> Optional[Dict[str, Any]]:
    candidates = credentials
    if signing_algorithm:
        matching_algorithm = [item for item in credentials if item["algorithm"] == signing_algorithm]
        if matching_algorithm:
            candidates = matching_algorithm
    for credential in sorted(candidates, key=lambda item: item["generation"]):
        if credential["digest"] == signature:
            return _credential_ref(credential)
    # Algorithm hint may be absent or inconsistent; still trace any exact digest.
    if signing_algorithm:
        for credential in credentials:
            if credential["digest"] == signature:
                return _credential_ref(credential)
    return None


def evaluate_token(
    token_id: str,
    tokens_by_id: Dict[str, Dict[str, Any]],
    credentials_by_user: Dict[str, List[Dict[str, Any]]],
    user_decisions: Dict[str, Dict[str, Any]],
    policy_view: Any,
    *,
    ledger_consumed: bool = False,
    ledger_use_count: int = 0,
) -> Dict[str, Any]:
    token = tokens_by_id.get(token_id)
    if token is None:
        return {
            "token_id": token_id,
            "status": "NOT_FOUND",
            "allowed": False,
            "code": E_TOKEN_NOT_FOUND,
        }

    base = {
        "token_id": token_id,
        "user_id": token["user_id"],
        "issued_at_ms": token["issued_at_ms"],
        "expires_at_ms": token["expires_at_ms"],
        "one_time": token["one_time"],
        "input_use_count": token["use_count"],
    }
    observed = policy_view.observed_clock_ms

    if not token["one_time"]:
        return {**base, "status": "DENIED", "allowed": False, "code": E_NOT_ONE_TIME}

    consumed = ledger_consumed or ledger_use_count > 0
    if token["use_count"] > 0 or consumed:
        return {
            **base,
            "status": "REUSED",
            "allowed": False,
            "code": E_TOKEN_REUSED,
            "ledger_consumed": ledger_consumed,
            "ledger_use_count": ledger_use_count,
        }

    if observed >= token["expires_at_ms"]:
        return {**base, "status": "EXPIRED", "allowed": False, "code": E_TOKEN_EXPIRED, "observed_clock_ms": observed}
    if observed < token["issued_at_ms"]:
        return {**base, "status": "NOT_YET_VALID", "allowed": False, "code": E_TOKEN_NOT_YET_VALID, "observed_clock_ms": observed}

    window = policy_view.issuance_in_current_window(token["user_id"], token["issued_at_ms"])
    if window["status"] == "CONFLICT":
        return {**base, "status": "POLICY_CONFLICT", "allowed": False, "code": E_POLICY_CONFLICT, "basis": window.get("basis")}
    if window["status"] != "OK":
        return {
            **base,
            "status": "OUTSIDE_POLICY_WINDOW",
            "allowed": False,
            "code": window.get("code", "E_OUTSIDE_POLICY_WINDOW"),
            "window_detail": {
                "current": _basis_summary(window.get("current")),
                "at_issue": _basis_summary(window.get("at_issue")),
            },
        }

    user_decision = user_decisions.get(token["user_id"])
    if user_decision is None:
        user_decision = evaluate_user(token["user_id"], credentials_by_user, policy_view)
    if user_decision["code"] == E_USER_NOT_FOUND:
        return {**base, "status": "USER_NOT_FOUND", "allowed": False, "code": E_USER_NOT_FOUND}
    if user_decision["code"] == E_POLICY_CONFLICT:
        return {**base, "status": "POLICY_CONFLICT", "allowed": False, "code": E_POLICY_CONFLICT, "basis": user_decision.get("basis")}

    credentials = credentials_by_user.get(token["user_id"], [])
    current_credential = choose_current_credential(credentials) if credentials else None
    signing_algorithm = token.get("signing_algorithm")
    if (
        current_credential is not None
        and user_decision.get("current_algorithm_is_new_tier")
        and signing_algorithm
        and not ALGORITHM_CATALOG[signing_algorithm].get("new_tier", True)
        and current_credential.get("effective_at_ms") is not None
        and token["issued_at_ms"] >= current_credential["effective_at_ms"]
    ):
        source = _find_signature_source(token["signature"], credentials, signing_algorithm)
        return {
            **base,
            "status": "OLD_TIER_AFTER_MIGRATION",
            "allowed": False,
            "code": E_OLD_TIER_AFTER_MIGRATION,
            "signature_source": source,
            "basis_policy_generation": user_decision.get("basis_policy_generation"),
            "current_credential": user_decision.get("current_credential"),
        }

    source = _find_signature_source(token["signature"], credentials, signing_algorithm)
    if source is None:
        return {
            **base,
            "status": "SIGNATURE_MISMATCH",
            "allowed": False,
            "code": E_SIGNATURE_MISMATCH,
            "basis_policy_generation": user_decision.get("basis_policy_generation"),
        }

    return {
        **base,
        "status": "ALLOWED",
        "allowed": True,
        "code": OK,
        "signature_source": source,
        "basis_policy_key": user_decision.get("basis_policy_key"),
        "basis_policy_generation": user_decision.get("basis_policy_generation"),
        "migration_status": user_decision.get("status"),
    }


def _basis_summary(resolution: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not resolution:
        return None
    return {
        "status": resolution.get("status"),
        "code": resolution.get("code"),
        "policy_key": resolution.get("policy_key"),
        "generation": resolution.get("generation"),
        "effective_at_ms": resolution.get("effective_at_ms"),
    }


def evaluate_all_tokens(
    tokens: Iterable[Dict[str, Any]],
    tokens_by_id: Dict[str, Dict[str, Any]],
    credentials_by_user: Dict[str, List[Dict[str, Any]]],
    user_decisions: Dict[str, Dict[str, Any]],
    policy_view: Any,
    *,
    ledger_tokens: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    ledger_tokens = ledger_tokens or {}
    results = []
    for token in sorted(tokens, key=lambda item: item["token_id"]):
        ledger_row = ledger_tokens.get(token["token_id"], {})
        results.append(
            evaluate_token(
                token["token_id"],
                tokens_by_id,
                credentials_by_user,
                user_decisions,
                policy_view,
                ledger_consumed=bool(ledger_row.get("consumed")),
                ledger_use_count=int(ledger_row.get("use_count") or 0),
            )
        )
    return results
