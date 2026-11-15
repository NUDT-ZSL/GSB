from __future__ import annotations

from typing import Any, Mapping

from .canonical import canonical_json, sha256_value
from .report import finalize_report

COMPARISON_SCHEMA_VERSION = "1.0"


def _matches_by_id(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    matches = report.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError("report.matches must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for match in matches:
        if not isinstance(match, dict) or not isinstance(match.get("match_id"), str):
            raise ValueError("each report match must have a string match_id")
        result[match["match_id"]] = match
    return result


def _traces_by_seq(match: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    traces = match.get("traces", [])
    if not isinstance(traces, list):
        raise ValueError("match.traces must be a list")
    result: dict[int, Mapping[str, Any]] = {}
    for trace in traces:
        if not isinstance(trace, dict) or not isinstance(trace.get("seq"), int):
            raise ValueError("each trace must have an integer seq")
        seq = int(trace["seq"])
        if seq in result:
            raise ValueError(f"duplicate trace seq {seq} in a match")
        result[seq] = trace
    return result


def _trace_context(trace: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if trace is None:
        return None
    return {
        "seq": trace.get("seq"),
        "status": trace.get("status"),
        "processed": trace.get("processed"),
        "action": trace.get("action"),
        "actor": trace.get("actor"),
        "state_hash": trace.get("state_hash"),
        "state": trace.get("state"),
        "rng_hash": trace.get("rng_hash"),
        "errors": trace.get("errors", []),
    }


def _match_signature(match: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: match.get(key)
        for key in (
            "status",
            "events_received",
            "events_processed",
            "rejected_events",
            "errors",
            "trigger_rollbacks",
            "final_state",
            "final_state_hash",
            "traces",
        )
    }


def compare_reports(
    left_report: Mapping[str, Any],
    right_report: Mapping[str, Any],
) -> dict[str, Any]:
    left_matches = _matches_by_id(left_report)
    right_matches = _matches_by_id(right_report)
    match_ids = sorted(set(left_matches) | set(right_matches))

    all_match_sets_equal = set(left_matches) == set(right_matches)
    all_event_key_sets_equal = all_match_sets_equal
    all_state_hashes_equal = all_match_sets_equal
    all_final_states_equal = all_match_sets_equal
    all_traces_equal = all_match_sets_equal
    first_state_divergence: dict[str, Any] | None = None
    first_trace_divergence: dict[str, Any] | None = None
    match_level_divergences: list[dict[str, Any]] = []

    for match_id in match_ids:
        left_match = left_matches.get(match_id)
        right_match = right_matches.get(match_id)
        if left_match is None or right_match is None:
            all_event_key_sets_equal = False
            all_state_hashes_equal = False
            all_final_states_equal = False
            all_traces_equal = False
            missing_side = "left" if left_match is None else "right"
            existing = right_match if left_match is None else left_match
            existing_traces = _traces_by_seq(existing)
            first_seq = min(existing_traces) if existing_traces else None
            left_trace = (
                _traces_by_seq(left_match).get(first_seq)
                if left_match is not None and first_seq is not None
                else None
            )
            right_trace = (
                _traces_by_seq(right_match).get(first_seq)
                if right_match is not None and first_seq is not None
                else None
            )
            divergence = {
                "match_id": match_id,
                "seq": first_seq,
                "kind": "missing_match",
                "missing_side": missing_side,
                "left": _trace_context(left_trace),
                "right": _trace_context(right_trace),
            }
            if first_trace_divergence is None:
                first_trace_divergence = divergence
            continue

        left_traces = _traces_by_seq(left_match)
        right_traces = _traces_by_seq(right_match)
        seqs = sorted(set(left_traces) | set(right_traces))
        if set(left_traces) != set(right_traces):
            all_event_key_sets_equal = False
            all_state_hashes_equal = False
            all_traces_equal = False

        for seq in seqs:
            left_trace = left_traces.get(seq)
            right_trace = right_traces.get(seq)
            if left_trace is None or right_trace is None:
                divergence = {
                    "match_id": match_id,
                    "seq": seq,
                    "kind": "missing_event",
                    "missing_side": "left" if left_trace is None else "right",
                    "left": _trace_context(left_trace),
                    "right": _trace_context(right_trace),
                }
                if first_trace_divergence is None:
                    first_trace_divergence = divergence
                continue

            state_payload_equal = canonical_json(left_trace.get("state")) == canonical_json(
                right_trace.get("state")
            )
            state_hash_equal = (
                left_trace.get("state_hash") == right_trace.get("state_hash")
                and state_payload_equal
            )
            if not state_hash_equal:
                if first_state_divergence is None:
                    first_state_divergence = {
                        "match_id": match_id,
                        "seq": seq,
                        "kind": "state_hash_mismatch",
                        "left_state_hash": left_trace.get("state_hash"),
                        "right_state_hash": right_trace.get("state_hash"),
                        "left_state": left_trace.get("state"),
                        "right_state": right_trace.get("state"),
                        "left_event": _trace_context(left_trace),
                        "right_event": _trace_context(right_trace),
                    }
                all_state_hashes_equal = False

            if canonical_json(left_trace) != canonical_json(right_trace):
                all_traces_equal = False
                if first_trace_divergence is None:
                    first_trace_divergence = {
                        "match_id": match_id,
                        "seq": seq,
                        "kind": "event_trace_mismatch",
                        "left": _trace_context(left_trace),
                        "right": _trace_context(right_trace),
                        "left_rng_stream": left_trace.get("rng_stream"),
                        "right_rng_stream": right_trace.get("rng_stream"),
                    }

        if left_match.get("final_state_hash") != right_match.get("final_state_hash"):
            all_final_states_equal = False
        left_final = canonical_json(left_match.get("final_state"))
        right_final = canonical_json(right_match.get("final_state"))
        if left_final != right_final:
            all_final_states_equal = False

        if canonical_json(_match_signature(left_match)) != canonical_json(
            _match_signature(right_match)
        ):
            all_traces_equal = False
            match_level_divergences.append(
                {
                    "match_id": match_id,
                    "seq": None,
                    "kind": "match_metadata_mismatch",
                    "left": {
                        "status": left_match.get("status"),
                        "events_processed": left_match.get("events_processed"),
                        "errors": left_match.get("errors", []),
                        "final_state_hash": left_match.get("final_state_hash"),
                    },
                    "right": {
                        "status": right_match.get("status"),
                        "events_processed": right_match.get("events_processed"),
                        "errors": right_match.get("errors", []),
                        "final_state_hash": right_match.get("final_state_hash"),
                    },
                }
            )
            if first_trace_divergence is None:
                first_trace_divergence = match_level_divergences[-1]

    all_match_sets_equal = set(left_matches) == set(right_matches)
    all_event_key_sets_equal = all_event_key_sets_equal and all_match_sets_equal
    all_state_hashes_equal = (
        all_state_hashes_equal
        and all_match_sets_equal
        and all_event_key_sets_equal
    )
    all_final_states_equal = all_final_states_equal and all_match_sets_equal
    all_traces_equal = (
        all_traces_equal
        and all_match_sets_equal
        and all_event_key_sets_equal
        and not match_level_divergences
    )
    consistent = bool(
        all_match_sets_equal
        and all_event_key_sets_equal
        and all_state_hashes_equal
        and all_final_states_equal
        and all_traces_equal
        and first_state_divergence is None
        and first_trace_divergence is None
    )

    if consistent:
        verdict = "consistent"
        message = "ALL_MATCHES_AND_ALL_EVENT_STATE_HASHES_MATCH"
    elif all_state_hashes_equal and all_event_key_sets_equal:
        verdict = "divergent"
        message = "ALL_STATE_HASHES_MATCH_BUT_TRACE_CONTENT_DIFFERS"
    elif not all_event_key_sets_equal:
        verdict = "divergent"
        message = "REPLAY_EVENT_OR_MATCH_KEYS_DIFFER"
    else:
        verdict = "divergent"
        message = "FIRST_STATE_HASH_DIVERGENCE_FOUND"

    comparison: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "verdict": verdict,
        "message": message,
        "consistent": consistent,
        "checks": {
            "match_sets_equal": all_match_sets_equal,
            "event_key_sets_equal": all_event_key_sets_equal,
            "all_event_state_hashes_equal": all_state_hashes_equal,
            "all_event_traces_equal": all_traces_equal,
            "all_final_states_equal": all_final_states_equal,
        },
        "counts": {
            "left_matches": len(left_matches),
            "right_matches": len(right_matches),
            "left_events": sum(len(_traces_by_seq(match)) for match in left_matches.values()),
            "right_events": sum(
                len(_traces_by_seq(match)) for match in right_matches.values()
            ),
        },
        "input_report_hashes": {
            "left": left_report.get("content_hash"),
            "right": right_report.get("content_hash"),
        },
        "first_state_divergence": first_state_divergence,
        "first_trace_divergence": first_trace_divergence,
        "match_level_divergences": match_level_divergences,
        "conclusion": {
            "all_matches_checked": True,
            "all_events_checked": True,
            "state_hash_sequence_checked_in_full": True,
            "trace_sequence_checked_in_full": True,
            "equivalent": consistent,
        },
    }
    return finalize_report(comparison)


def comparison_sha256(comparison: Mapping[str, Any]) -> str:
    payload = dict(comparison)
    payload.pop("content_hash", None)
    return sha256_value(payload)
