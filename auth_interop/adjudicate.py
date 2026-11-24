from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .codes import (
    E_CLIENT_MISMATCH,
    E_CODE_EXPIRED,
    E_CODE_NOT_ACTIVE,
    E_CODE_REPLAY,
    E_CODE_REUSED,
    E_CODE_UNKNOWN,
    E_ENTITY_TOMBSTONED,
    E_PKCE_DOWNGRADE,
    E_PKCE_MISMATCH,
    E_STATE_CONFLICT,
    OK,
)
from .io_utils import read_json_file
from .merge import MergedView
from .pkce import verify_pkce


JsonDict = dict[str, Any]


@dataclass
class RedemptionAttempt:
    attempt_id: str
    code: str
    client_id: Optional[str]
    redirect_uri: Optional[str]
    code_verifier: Optional[str]
    sequence: int
    source: JsonDict

    @classmethod
    def from_raw(cls, raw: JsonDict, path: str, index: int) -> "RedemptionAttempt":
        attempt_id = str(raw.get("attempt_id") or raw.get("id") or f"attempt-{index}")
        return cls(
            attempt_id=attempt_id,
            code=str(raw.get("code") or raw.get("authorization_code") or "").strip(),
            client_id=_none_if_blank(raw.get("client_id") or raw.get("clientId")),
            redirect_uri=_none_if_blank(raw.get("redirect_uri") or raw.get("redirectUri")),
            code_verifier=_none_if_blank(raw.get("code_verifier") or raw.get("codeVerifier")),
            sequence=int(raw.get("sequence", index)),
            source={
                "source_version": "redemption-input",
                "path": path.replace("\\", "/"),
                "record_no": index,
                "record_index": index - 1,
                "record_id": attempt_id,
            },
        )


def _none_if_blank(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_attempts(path: str | Path) -> list[RedemptionAttempt]:
    p = Path(path)
    data = read_json_file(p)
    if isinstance(data, dict):
        raw_attempts = data.get("attempts", [])
    else:
        raw_attempts = data
    if not isinstance(raw_attempts, list):
        raise ValueError("redemption input must be a list or contain an attempts array")
    display = str(p).replace("\\", "/")
    return [
        RedemptionAttempt.from_raw(raw, display, index)
        for index, raw in enumerate(raw_attempts, start=1)
        if isinstance(raw, dict)
    ]


def adjudicate_attempts(
    view: MergedView,
    attempts: list[RedemptionAttempt],
    observed_at_ms: int,
) -> JsonDict:
    first_seen: dict[str, RedemptionAttempt] = {}
    results: list[JsonDict] = []

    ordered_attempts = sorted(attempts, key=lambda item: (item.sequence, item.source["record_no"]))
    for attempt in ordered_attempts:
        replay_first = first_seen.get(attempt.code)
        result = _adjudicate_one(view, attempt, observed_at_ms, replay_first)
        results.append(result)
        first_seen.setdefault(attempt.code, attempt)

    summary = _summary(results)
    return {
        "adjudication": "authorization_code_redemption",
        "observed_at_ms": observed_at_ms,
        "time_source": "samples/clock.json#observed_at_ms",
        "summary": summary,
        "results": results,
    }


def _adjudicate_one(
    view: MergedView,
    attempt: RedemptionAttempt,
    observed_at_ms: int,
    replay_first: Optional[RedemptionAttempt],
) -> JsonDict:
    checks: dict[str, JsonDict] = {}
    codes: list[str] = []
    entity_key: Optional[str] = None
    record = None
    entity = None

    keys = view.code_index().get(attempt.code, []) if attempt.code else []
    if not attempt.code:
        primary = E_CODE_UNKNOWN
        checks["code_present"] = {"ok": False, "code": E_CODE_UNKNOWN}
    elif not keys:
        primary = E_CODE_UNKNOWN
        checks["entity_lookup"] = {"ok": False, "code": E_CODE_UNKNOWN, "code_value": attempt.code}
    elif len(keys) > 1:
        primary = E_STATE_CONFLICT
        codes.append(E_STATE_CONFLICT)
        checks["entity_identity"] = {
            "ok": False,
            "code": E_STATE_CONFLICT,
            "candidate_keys": keys,
        }
    else:
        entity_key = keys[0]
        entity = view.entities[entity_key]
        record = entity.authoritative

    replay_trace = None
    if replay_first is not None:
        codes.append(E_CODE_REPLAY)
        replay_trace = {
            "code": E_CODE_REPLAY,
            "first_exchange": replay_first.source,
            "second_exchange": attempt.source,
            "first_attempt_id": replay_first.attempt_id,
            "second_attempt_id": attempt.attempt_id,
        }
        checks["replay"] = {"ok": False, "code": E_CODE_REPLAY, "trace": replay_trace}
    else:
        checks["replay"] = {"ok": True, "code": OK}

    if record is not None and entity is not None:
        if entity.status == "tombstoned":
            codes.append(E_ENTITY_TOMBSTONED)
            checks["tombstone"] = {
                "ok": False,
                "code": E_ENTITY_TOMBSTONED,
                "source": record.source_dict(),
            }
        elif entity.status == "conflict":
            codes.append(E_STATE_CONFLICT)
            checks["merge_state"] = {
                "ok": False,
                "code": E_STATE_CONFLICT,
                "conflicts": entity.conflicts,
            }

        checks["time_window"] = _check_time(record.issued_at_ms, record.expires_at_ms, observed_at_ms)
        if not checks["time_window"]["ok"]:
            codes.append(checks["time_window"]["code"])

        reused = bool(record.used or record.used_at_ms is not None)
        checks["unused"] = {
            "ok": not reused,
            "code": OK if not reused else E_CODE_REUSED,
            "used": reused,
            "used_at_ms": record.used_at_ms,
            "source": record.source_dict(),
        }
        if reused and replay_first is None:
            codes.append(E_CODE_REUSED)

        client_ok = attempt.client_id == record.client_id
        checks["client_id"] = {
            "ok": client_ok,
            "code": OK if client_ok else E_CLIENT_MISMATCH,
            "expected": record.client_id,
            "provided": attempt.client_id,
            "source": record.source_dict(),
        }
        if not client_ok:
            codes.append(E_CLIENT_MISMATCH)

        redirect_ok = True
        if record.redirect_uri:
            redirect_ok = attempt.redirect_uri == record.redirect_uri
        checks["redirect_uri"] = {
            "ok": redirect_ok,
            "code": OK if redirect_ok else E_CLIENT_MISMATCH,
            "field": "redirect_uri",
            "expected": record.redirect_uri,
            "provided": attempt.redirect_uri,
            "source": record.source_dict(),
        }
        if not redirect_ok:
            codes.append(E_CLIENT_MISMATCH)

        pkce_ok, pkce_reason = verify_pkce(
            record.pkce_method, record.pkce_challenge, attempt.code_verifier
        )
        if not record.pkce_challenge:
            pkce_code = OK
        elif pkce_reason == "missing_verifier":
            pkce_code = E_PKCE_DOWNGRADE
            codes.append(E_PKCE_DOWNGRADE)
        elif not pkce_ok:
            pkce_code = E_PKCE_MISMATCH
            codes.append(E_PKCE_MISMATCH)
        else:
            pkce_code = OK
        checks["pkce"] = {
            "ok": pkce_ok if record.pkce_challenge else True,
            "code": pkce_code,
            "reason": pkce_reason,
            "method": record.pkce_method,
            "challenge_declared": bool(record.pkce_challenge),
            "verifier_provided": bool(attempt.code_verifier),
            "source": record.source_dict(),
        }

        lineage_downgrades = [
            finding for finding in entity.findings if finding.get("code") == E_PKCE_DOWNGRADE
        ]
        if lineage_downgrades:
            codes.append(E_PKCE_DOWNGRADE)
        checks["pkce_lineage"] = {
            "ok": not lineage_downgrades,
            "code": OK if not lineage_downgrades else E_PKCE_DOWNGRADE,
            "findings": lineage_downgrades,
        }

    primary = _primary_code(codes)
    verdict = "allowed" if primary == OK else "denied"
    result = {
        "attempt_id": attempt.attempt_id,
        "entity_key": entity_key,
        "code_value": attempt.code,
        "verdict": verdict,
        "conclusion": primary,
        "primary_code": primary,
        "business_code": primary,
        "observed_at_ms": observed_at_ms,
        "attempt_source": attempt.source,
        "checks": checks,
        "replay_trace": replay_trace,
        "lineage_findings": entity.findings if entity else [],
        "conflicts": entity.conflicts if entity else [],
        "basis": [record.source_dict()] if record else [],
        "generation": record.generation if record else None,
        "sources": [item.source_dict() for item in entity.records] if entity else [],
    }
    return result


def _check_time(issued: Optional[int], expires: Optional[int], observed: int) -> JsonDict:
    if issued is not None and observed < issued:
        return {
            "ok": False,
            "code": E_CODE_NOT_ACTIVE,
            "issued_at_ms": issued,
            "expires_at_ms": expires,
            "observed_at_ms": observed,
        }
    expired = expires is None or observed > expires
    return {
        "ok": not expired,
        "code": OK if not expired else E_CODE_EXPIRED,
        "issued_at_ms": issued,
        "expires_at_ms": expires,
        "observed_at_ms": observed,
        "window_inclusive": True,
    }


_PRIORITY = [
    E_STATE_CONFLICT,
    E_CODE_REPLAY,
    E_ENTITY_TOMBSTONED,
    E_CODE_EXPIRED,
    E_CODE_NOT_ACTIVE,
    E_CODE_REUSED,
    E_CLIENT_MISMATCH,
    E_PKCE_DOWNGRADE,
    E_PKCE_MISMATCH,
    E_CODE_UNKNOWN,
]


def _primary_code(codes: list[str]) -> str:
    unique = list(dict.fromkeys(codes))
    if not unique:
        return OK
    for candidate in _PRIORITY:
        if candidate in unique:
            return candidate
    return unique[0]


def _summary(results: list[JsonDict]) -> JsonDict:
    counts: dict[str, int] = {}
    allowed = 0
    for result in results:
        code = result["primary_code"]
        counts[code] = counts.get(code, 0) + 1
        if result["verdict"] == "allowed":
            allowed += 1
    return {
        "total": len(results),
        "allowed": allowed,
        "denied": len(results) - allowed,
        "all_succeeded": len(results) > 0 and allowed == len(results),
        "codes": dict(sorted(counts.items())),
    }
