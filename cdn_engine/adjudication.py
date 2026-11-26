from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


SEVERITY = {
    "RULE_NOT_FOUND": 100,
    "RULE_TOMBSTONED": 90,
    "ORIGIN_CONNECTION_REFUSED": 80,
    "DIGEST_MISMATCH": 70,
    "EDGE_PROBE_ERROR": 60,
    "STALE_OBSERVATION": 50,
    "MIXED_PROBE_RESULTS": 40,
    "CACHE_MISS_OBSERVED": 20,
    "CACHE_HIT_VERIFIED": 10,
    "CACHE_HIT": 10,
    "NO_OBSERVATION": 0,
}


def _observation_verdict(
    processed: Dict[str, Any], rule: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    origin = processed.get("origin_result") or {}
    result = processed["result"]
    verify_type = (rule or {}).get("verify_type", "status")

    if rule is None:
        code = "RULE_NOT_FOUND"
        consistent = False
        cache_hit = False
    elif "origin" in verify_type:
        if origin.get("error_code") == "ORIGIN_CONNECTION_REFUSED":
            code = "ORIGIN_CONNECTION_REFUSED"
            consistent = False
            cache_hit = False
        elif origin.get("digest_match") is True:
            code = "CACHE_HIT_VERIFIED"
            consistent = True
            cache_hit = True
        else:
            code = "DIGEST_MISMATCH"
            consistent = False
            cache_hit = False
    elif verify_type in {"content_digest", "digest"}:
        expected = rule.get("content_digest")
        actual = processed.get("content_digest")
        if expected and actual == expected:
            code = "CACHE_HIT_VERIFIED"
            consistent = True
            cache_hit = result == "hit"
        else:
            code = "DIGEST_MISMATCH"
            consistent = False
            cache_hit = False
    elif result in {"connection_refused", "timeout", "error"}:
        code = "EDGE_PROBE_ERROR"
        consistent = False
        cache_hit = False
    elif result == "digest_mismatch":
        code = "DIGEST_MISMATCH"
        consistent = False
        cache_hit = False
    elif result == "stale":
        code = "STALE_OBSERVATION"
        consistent = False
        cache_hit = False
    elif result == "miss":
        code = "CACHE_MISS_OBSERVED"
        consistent = True
        cache_hit = False
    elif result == "hit":
        code = "CACHE_HIT"
        consistent = True
        cache_hit = True
    else:
        code = "MIXED_PROBE_RESULTS"
        consistent = True
        cache_hit = False

    return {
        "seq": processed["seq"],
        "observation_id": processed.get("observation_id"),
        "claimed_result": result,
        "observed_digest": processed.get("content_digest"),
        "verdict_code": code,
        "consistent": consistent,
        "cache_hit": cache_hit,
        "origin_arbitration": origin if origin else None,
    }


def adjudicate_window(
    *,
    window: Dict[str, Any],
    rule: Optional[Dict[str, Any]],
    view: Optional[Dict[str, Any]],
    processed_rows: List[Any],
) -> Dict[str, Any]:
    processed: List[Dict[str, Any]] = []
    for row in processed_rows:
        item = dict(row)
        item["origin_result"] = loads(item.get("origin_json"), {})
        processed.append(item)

    observation_verdicts = [_observation_verdict(item, rule) for item in processed]
    if not observation_verdicts:
        overall_code = "NO_OBSERVATION"
        consistent: Optional[bool] = None
        cache_hit = False
    else:
        overall = max(
            observation_verdicts,
            key=lambda item: SEVERITY.get(item["verdict_code"], 0),
        )
        overall_code = overall["verdict_code"]
        if overall_code == "MIXED_PROBE_RESULTS":
            codes = {item["verdict_code"] for item in observation_verdicts}
            if codes == {"CACHE_HIT", "CACHE_MISS_OBSERVED"}:
                overall_code = "MIXED_HIT_AND_MISS"
        consistent = all(item["consistent"] for item in observation_verdicts)
        cache_hit = all(item["cache_hit"] for item in observation_verdicts) and any(
            item["cache_hit"] for item in observation_verdicts
        )
        if overall_code in {"DIGEST_MISMATCH", "ORIGIN_CONNECTION_REFUSED", "RULE_NOT_FOUND", "RULE_TOMBSTONED", "EDGE_PROBE_ERROR", "STALE_OBSERVATION"}:
            consistent = False
        if overall_code == "RULE_TOMBSTONED":
            consistent = False

    if rule and rule.get("tombstone") and observation_verdicts:
        overall_code = "RULE_TOMBSTONED"
        consistent = False

    return {
        "schema": "cdn-conclusion/v1",
        "window": {
            "window_id": window["window_id"],
            "view_id": window["view_id"],
            "view_uid": (view or {}).get("view_uid"),
            "node": window["node_name"],
            "rule_name": window["rule_name"],
            "start_ms": window["start_ms"],
            "end_ms": window["end_ms"],
            "recompute_of_window_id": window.get("recompute_of_window_id"),
        },
        "rule_version": {
            "view_id": window["view_id"],
            "view_uid": (view or {}).get("view_uid"),
            "source_format": (rule or {}).get("source_format"),
            "source_version": (rule or {}).get("source_version"),
            "generation": (rule or {}).get("generation"),
            "verify_type": (rule or {}).get("verify_type", "status"),
            "content_digest": (rule or {}).get("content_digest"),
        },
        "verdict_code": overall_code,
        "consistent": consistent,
        "consistency_state": "unknown" if consistent is None else ("consistent" if consistent else "inconsistent"),
        "cache_hit": cache_hit,
        "observation_count": len(observation_verdicts),
        "observation_seqs": [item["seq"] for item in processed],
        "observations": observation_verdicts,
        "decision_inputs": {
            "probe_results": [item["result"] for item in processed],
            "content_digests": [item.get("content_digest") for item in processed],
            "verify_type": (rule or {}).get("verify_type", "status"),
        },
    }
