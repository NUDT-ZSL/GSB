"""Deterministic window processing and four-state health machine."""
from __future__ import annotations

import json
import os
import signal
from decimal import Decimal
from typing import Any

from .errors import HSMError, InvalidStateTransition, NoActiveThreshold, ProbeError
from .scoring import combine_window_scores, level_score, window_bounds, window_index_for_clock
from .util import canonical_json, sha256_json


STATE_ORDER = {"healthy": 0, "degraded": 1, "lost": 2, "isolated": 3}


class GracefulTermination(HSMError):
    code = "HSM-5130"
    exit_code = 130


def fault_point(name: str) -> None:
    """Test-only crash hook.

    HSM_FAULT selects a hook. The default mode sends an uncatchable SIGKILL.
    HSM_FAULT_MODE=sigterm exercises the graceful termination path.
    """

    if os.environ.get("HSM_FAULT") != name:
        return
    mode = os.environ.get("HSM_FAULT_MODE", "sigkill")
    if mode == "sigterm":
        os.kill(os.getpid(), signal.SIGTERM)
        # A signal handler may set a flag instead of raising. Give it a point.
        raise GracefulTermination("graceful termination requested at " + name)
    os.kill(os.getpid(), signal.SIGKILL)
    os._exit(137)


def loads_json(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value, parse_float=Decimal)


def initial_state(instance_id: str) -> dict[str, Any]:
    state = {
        "instance_id": instance_id,
        "state": "healthy",
        "failure_streak": 0,
        "recovery_streak": 0,
        "recovery_arc": None,
        "recovery_start_clock_ms": None,
        "recovery_last_clock_ms": None,
        "recovery_duration_ms": 0,
        "last_window_index": None,
        "last_clock_ms": 0,
        "committed_seqs": {},
        "committed_stream_seq": 0,
        "threshold_revision": None,
        "threshold_hash": None,
        "threshold_generation": None,
        "state_version": 0,
        "updated_patch_id": None,
    }
    state["state_hash"] = hash_state(state)
    return state


def hash_state(state: dict[str, Any]) -> str:
    payload = {key: value for key, value in state.items() if key != "state_hash"}
    if isinstance(payload.get("recovery_arc"), dict) and "db_id" in payload["recovery_arc"]:
        payload["recovery_arc"] = {key: value for key, value in payload["recovery_arc"].items() if key != "db_id"}
    return sha256_json(payload)


def ensure_initial_state_conn(conn: Any, instance_id: str) -> None:
    state = initial_state(instance_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO instance_state
        (instance_id, state, failure_streak, recovery_streak, recovery_arc_json,
         recovery_start_clock_ms, recovery_last_clock_ms, recovery_duration_ms,
         last_window_index, last_clock_ms, committed_seqs_json, committed_stream_seq,
         threshold_revision, threshold_hash, threshold_generation, state_version, state_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state["instance_id"], state["state"], state["failure_streak"], state["recovery_streak"],
            canonical_json(state["recovery_arc"]), state["recovery_start_clock_ms"],
            state["recovery_last_clock_ms"], state["recovery_duration_ms"], state["last_window_index"],
            state["last_clock_ms"], canonical_json(state["committed_seqs"]), state["committed_stream_seq"],
            state["threshold_revision"], state["threshold_hash"], state["threshold_generation"],
            state["state_version"], state["state_hash"],
        ),
    )


def store_registry(store: Any, registry: dict[str, Any], registry_hash: str) -> dict[str, int]:
    with store.transaction("_registry") as conn:
        for service in registry["services"].values():
            conn.execute(
                "INSERT OR REPLACE INTO services(service_id, service_name, payload_json) VALUES (?, ?, ?)",
                (service["service_id"], service["service_name"], canonical_json(service)),
            )
        for instance in registry["instances"].values():
            conn.execute(
                """
                INSERT OR REPLACE INTO instances
                (instance_id, service_id, service_name, endpoint, state_generation,
                 levels_json, window_ms, timeout_ms, registry_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance["instance_id"], instance["service_id"], instance["service_name"],
                    instance["endpoint"], instance["state_generation"], canonical_json(instance["levels"]),
                    instance["window_ms"], instance["timeout_ms"], registry_hash,
                ),
            )
            state = initial_state(instance["instance_id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO instance_state
                (instance_id, state, failure_streak, recovery_streak, recovery_arc_json,
                 recovery_start_clock_ms, recovery_last_clock_ms, recovery_duration_ms,
                 last_window_index, last_clock_ms, committed_seqs_json, committed_stream_seq,
                 threshold_revision, threshold_hash, threshold_generation, state_version, state_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["instance_id"], state["state"], state["failure_streak"], state["recovery_streak"],
                    canonical_json(state["recovery_arc"]), state["recovery_start_clock_ms"],
                    state["recovery_last_clock_ms"], state["recovery_duration_ms"], state["last_window_index"],
                    state["last_clock_ms"], canonical_json(state["committed_seqs"]), state["committed_stream_seq"],
                    state["threshold_revision"], state["threshold_hash"], state["threshold_generation"],
                    state["state_version"], state["state_hash"],
                ),
            )
    return {"services": len(registry["services"]), "instances": len(registry["instances"])}


def store_observations(store: Any, observations: list[dict[str, Any]]) -> dict[str, int]:
    inserted = 0
    idempotent = 0
    with store.transaction("_observations") as conn:
        for observation in observations:
            instance = conn.execute(
                "SELECT window_ms FROM instances WHERE instance_id=?",
                (observation["instance_id"],),
            ).fetchone()
            if instance is None:
                raise ProbeError("observation references unloaded instance", location={"instance_id": observation["instance_id"]})
            window_index = window_index_for_clock(observation["clock_ms"], instance["window_ms"])
            payload = {
                "stream_seq": observation["stream_seq"],
                "instance_id": observation["instance_id"],
                "level": observation["level"],
                "seq": observation["seq"],
                "clock_ms": observation["clock_ms"],
                "successes": observation["successes"],
                "failures": observation["failures"],
                "avg_latency_ms": str(observation["avg_latency_ms"]),
            }
            content_hash = sha256_json(payload)
            existing = conn.execute(
                "SELECT content_hash, status FROM observation_inbox WHERE instance_id=? AND level=? AND seq=?",
                (observation["instance_id"], observation["level"], observation["seq"]),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    from .errors import ConcurrencyError

                    raise ConcurrencyError(
                        "same instance/level/seq was submitted with different observation content",
                        location={"instance_id": observation["instance_id"], "level": observation["level"]},
                        details={"seq": observation["seq"]},
                    )
                idempotent += 1
                continue
            conn.execute(
                """
                INSERT INTO observation_inbox
                (instance_id, level, seq, stream_seq, window_index, clock_ms, successes, failures,
                 latency_text, content_hash, raw_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received')
                """,
                (
                    observation["instance_id"], observation["level"], observation["seq"], observation["stream_seq"],
                    window_index, observation["clock_ms"], observation["successes"], observation["failures"],
                    str(observation["avg_latency_ms"]), content_hash, canonical_json(payload),
                ),
            )
            inserted += 1
    fault_point("after_inbox_commit")
    return {"inserted": inserted, "idempotent": idempotent, "total": len(observations)}


def _load_state(conn: Any, instance_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM instance_state WHERE instance_id=?", (instance_id,)).fetchone()
    if row is None:
        state = initial_state(instance_id)
        return state
    result = dict(row)
    result["committed_seqs"] = loads_json(result.pop("committed_seqs_json"), {})
    result["recovery_arc"] = loads_json(result.pop("recovery_arc_json"), None)
    result["state_version"] = result["state_version"]
    return result


def _save_state(conn: Any, state: dict[str, Any]) -> None:
    state["state_hash"] = hash_state(state)
    recovery_arc = state.get("recovery_arc")
    if isinstance(recovery_arc, dict) and "db_id" in recovery_arc:
        recovery_arc = {key: value for key, value in recovery_arc.items() if key != "db_id"}
    conn.execute(
        """
        UPDATE instance_state SET state=?, failure_streak=?, recovery_streak=?, recovery_arc_json=?,
            recovery_start_clock_ms=?, recovery_last_clock_ms=?, recovery_duration_ms=?,
            last_window_index=?, last_clock_ms=?, committed_seqs_json=?, committed_stream_seq=?,
            threshold_revision=?, threshold_hash=?, threshold_generation=?, state_version=?,
            state_hash=?, updated_patch_id=?
        WHERE instance_id=?
        """,
        (
            state["state"], state["failure_streak"], state["recovery_streak"], canonical_json(recovery_arc),
            state["recovery_start_clock_ms"], state["recovery_last_clock_ms"], state["recovery_duration_ms"],
            state["last_window_index"], state["last_clock_ms"], canonical_json(state["committed_seqs"]),
            state["committed_stream_seq"], state["threshold_revision"], state["threshold_hash"],
            state["threshold_generation"], state["state_version"], state["state_hash"],
            state.get("updated_patch_id"), state["instance_id"],
        ),
    )


def _policy(conn: Any) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM meta WHERE key='state_policy'").fetchone()
    if row is None:
        from .thresholds import DEFAULT_STATE_POLICY

        return dict(DEFAULT_STATE_POLICY)
    return loads_json(row["value"])


def _threshold_views(conn: Any, instance_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM threshold_view WHERE instance_id=? ORDER BY level", (instance_id,)).fetchall()
    if not rows:
        raise NoActiveThreshold("merged threshold view is missing; run merge first", location={"instance_id": instance_id})
    views = []
    for row in rows:
        view = dict(row)
        view["decision"] = loads_json(view.pop("decision_json"), {})
        view["provenance"] = loads_json(view.pop("provenance_json"), {})
        view["time_conversion"] = loads_json(view.pop("time_conversion_json"), {})
        view["raw_failure_counts"] = loads_json(view.pop("raw_failure_counts_json"), [])
        views.append(view)
    return views


def _next_transition_hash(conn: Any, instance_id: str, payload: dict[str, Any]) -> tuple[int, str, str]:
    row = conn.execute(
        "SELECT transition_seq, entry_hash FROM transition_log WHERE instance_id=? ORDER BY transition_seq DESC LIMIT 1",
        (instance_id,),
    ).fetchone()
    transition_seq = 1 if row is None else row["transition_seq"] + 1
    prev_hash = "0" * 64 if row is None else row["entry_hash"]
    entry = {"transition_seq": transition_seq, "prev_hash": prev_hash, **payload}
    return transition_seq, prev_hash, sha256_json(entry)


def adjudicate_state(
    previous_state: str,
    *,
    failed: bool,
    score_bp: int,
    failure_streak: int,
    recovery_streak: int,
    recovery_duration_ms: int,
    policy: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(new_state, reason)`` without touching persistence."""

    if failed:
        if failure_streak >= policy["isolated_after_failures"]:
            candidate = "isolated"
        elif failure_streak >= policy["lost_after_failures"]:
            candidate = "lost"
        elif failure_streak >= policy["degraded_after_failures"]:
            candidate = "degraded"
        else:
            candidate = "healthy"
        target = max((previous_state, candidate), key=lambda name: STATE_ORDER[name])
        if target != previous_state:
            return target, f"failure_streak_{failure_streak}_crosses_{target}_threshold"
        return previous_state, f"failure_streak_{failure_streak}_holds_{previous_state}"

    recovery_quality = score_bp >= policy["healthy_score_bp"]
    if not recovery_quality:
        return previous_state, "low_score_without_joint_breach_holds_state"

    if previous_state == "isolated":
        enough = (
            recovery_streak >= policy["isolated_recovery_windows"]
            and recovery_duration_ms >= policy["isolated_recovery_duration_ms"]
        )
        return ("healthy", "isolated_recovery_threshold_met") if enough else (previous_state, "isolated_recovery_arc_in_progress")
    if previous_state == "lost":
        enough = (
            recovery_streak >= policy["lost_recovery_windows"]
            and recovery_duration_ms >= policy["lost_recovery_duration_ms"]
        )
        return ("healthy", "lost_recovery_threshold_met") if enough else (previous_state, "lost_recovery_arc_in_progress")
    if previous_state == "degraded":
        enough = (
            recovery_streak >= policy["degraded_recovery_windows"]
            and recovery_duration_ms >= policy["degraded_recovery_duration_ms"]
        )
        return ("healthy", "degraded_recovery_threshold_met") if enough else (previous_state, "degraded_recovery_arc_in_progress")
    return "healthy", "steady_healthy_window"


def _close_recovery_arc(conn: Any, state: dict[str, Any], *, status: str, result_state: str | None) -> None:
    arc = state.get("recovery_arc")
    if not arc or arc.get("arc_seq") is None:
        return
    conn.execute(
        """
        UPDATE recovery_arcs SET status=?, ended_clock_ms=?, duration_ms=?, result_state=?
        WHERE instance_id=? AND arc_seq=?
        """,
        (
            status,
            state.get("recovery_last_clock_ms"),
            state.get("recovery_duration_ms", 0),
            result_state,
            state["instance_id"],
            arc["arc_seq"],
        ),
    )


def _commit_window(conn: Any, store: Store, instance_id: str, window_index: int) -> dict[str, Any]:
    instance_row = conn.execute("SELECT * FROM instances WHERE instance_id=?", (instance_id,)).fetchone()
    if instance_row is None:
        raise ProbeError("unknown instance", location={"instance_id": instance_id})
    instance = dict(instance_row)
    state = _load_state(conn, instance_id)
    policy = _policy(conn)
    views = _threshold_views(conn, instance_id)
    view_by_level = {view["level"]: view for view in views}
    pointer = conn.execute("SELECT * FROM unified_pointer WHERE instance_id=?", (instance_id,)).fetchone()
    if pointer is None:
        raise NoActiveThreshold("unified threshold pointer missing", location={"instance_id": instance_id})

    rows = conn.execute(
        "SELECT * FROM observation_inbox WHERE instance_id=? AND window_index=? ORDER BY level, seq",
        (instance_id, window_index),
    ).fetchall()
    if not rows:
        raise ProbeError("window has no observations", location={"instance_id": instance_id, "path": f"window:{window_index}"})

    window_start, window_end = window_bounds(window_index, instance["window_ms"])
    by_level: dict[int, list[Any]] = {}
    for row in rows:
        by_level.setdefault(row["level"], []).append(row)

    level_results: list[dict[str, Any]] = []
    evidence_by_level: dict[str, Any] = {}
    trigger_candidates: list[dict[str, Any]] = []
    timeout_candidates: list[dict[str, Any]] = []

    for level, level_rows in sorted(by_level.items()):
        view = view_by_level.get(level)
        successes = sum(row["successes"] for row in level_rows)
        failures = sum(row["failures"] for row in level_rows)
        latency_weighted = Decimal(0)
        latency_denominator = 0
        latency_values: list[Decimal] = []
        for row in level_rows:
            latency = Decimal(row["latency_text"])
            total = row["successes"] + row["failures"]
            latency_values.append(latency)
            if total > 0:
                latency_weighted += latency * total
                latency_denominator += total
        if latency_denominator > 0:
            avg_latency = latency_weighted / latency_denominator
        elif latency_values:
            avg_latency = sum(latency_values, Decimal(0)) / Decimal(len(latency_values))
        else:
            avg_latency = Decimal(0)

        current_min_clock = min(row["clock_ms"] for row in level_rows)
        current_max_clock = max(row["clock_ms"] for row in level_rows)
        previous = conn.execute(
            """
            SELECT clock_ms FROM observation_inbox
            WHERE instance_id=? AND level=? AND window_index < ?
            ORDER BY clock_ms DESC, seq DESC LIMIT 1
            """,
            (instance_id, level, window_index),
        ).fetchone()
        gap = None if previous is None else current_min_clock - previous["clock_ms"]
        is_timeout = gap is not None and gap > instance["timeout_ms"]

        raw_evidence = [json.loads(row["raw_json"], parse_float=Decimal) for row in level_rows]
        evidence_by_level[str(level)] = raw_evidence
        if view is None or not view["active"]:
            continue

        score = level_score(
            successes=successes,
            failures=failures,
            avg_latency_ms=avg_latency,
            weight=view["weight"],
            min_score_bp=view["min_score_bp"],
            latency_budget_ms=Decimal(view["latency_budget_ms"]),
        )
        threshold_breach = failures >= view["failure_limit"]
        joint_breach = score["score_breach"] and threshold_breach
        score.update(
            {
                "level": level,
                "threshold_breach": threshold_breach,
                "joint_breach": joint_breach,
                "timeout_breach": is_timeout,
                "previous_clock_ms": None if previous is None else previous["clock_ms"],
                "current_clock_ms": current_min_clock,
                "clock_gap_ms": gap,
                "avg_latency_ms": str(avg_latency),
                "failure_limit": view["failure_limit"],
            }
        )
        level_results.append(score)
        conn.execute(
            """
            INSERT OR REPLACE INTO window_level_scores
            (instance_id, window_index, level, score_bp, availability_bp, latency_score_bp, weight,
             successes, failures, total_probes, latency_text, breached, timeout_breached,
             observation_seqs_json, detail_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id, window_index, level, score["score_bp"], score["availability_bp"],
                score["latency_score_bp"], score["weight"], successes, failures, score["total_probes"],
                str(avg_latency), 1 if joint_breach else 0, 1 if is_timeout else 0,
                canonical_json([row["seq"] for row in level_rows]), canonical_json(score),
            ),
        )
        if joint_breach:
            total = max(1, successes + failures)
            trigger_candidates.append(
                {
                    "level": level,
                    "failure_ratio": failures / total,
                    "weight": view["weight"],
                    "score_bp": score["score_bp"],
                    "failure_limit": view["failure_limit"],
                    "actual_failures": failures,
                    "min_score_bp": view["min_score_bp"],
                }
            )
        if is_timeout:
            timeout_candidates.append({"level": level, "gap_ms": gap, "timeout_ms": instance["timeout_ms"]})

    excluded: list[dict[str, Any]] = []
    for view in views:
        if not view["active"]:
            excluded.append(
                {
                    "level": view["level"],
                    "generation": view["excluded_generation"] if view["excluded_generation"] is not None else view["generation"],
                    "reason": view["exclusion_reason"] or "inactive",
                }
            )
        for loser in view.get("decision", {}).get("losers", []):
            excluded.append(
                {
                    "level": view["level"],
                    "generation": loser["generation"],
                    "source_format": loser["source_format"],
                    "reason": "superseded_by_" + view["decision"].get("reason", "newer_version"),
                }
            )

    if not level_results:
        raise NoActiveThreshold(
            "window observations only reference excluded thresholds",
            location={"instance_id": instance_id},
            details={"window_index": window_index, "excluded": excluded},
        )

    score_bp = combine_window_scores(level_results)
    primary_trigger: dict[str, Any] | None = None
    trigger_kind = "none"
    trigger_level: int | None = None
    trigger_threshold: dict[str, Any] = {}
    failed = False
    timed_out = bool(timeout_candidates)

    if trigger_candidates:
        failed = True
        trigger_candidates.sort(key=lambda item: (-item["failure_ratio"], -item["weight"], item["level"]))
        chosen = trigger_candidates[0]
        trigger_level = chosen["level"]
        trigger_kind = "joint_score_and_failure_threshold"
        trigger_threshold = {
            "kind": trigger_kind,
            "level": trigger_level,
            "min_score_bp": chosen["min_score_bp"],
            "actual_score_bp": chosen["score_bp"],
            "failure_limit": chosen["failure_limit"],
            "actual_failures": chosen["actual_failures"],
            "candidates": trigger_candidates,
        }
        primary_trigger = chosen
    elif timeout_candidates:
        failed = True
        timeout_candidates.sort(key=lambda item: (-item["gap_ms"], item["level"]))
        chosen = timeout_candidates[0]
        trigger_level = chosen["level"]
        trigger_kind = "injected_clock_timeout"
        trigger_threshold = {"kind": trigger_kind, **chosen}

    previous_state = state["state"]
    failure_streak = state["failure_streak"]
    recovery_streak = state["recovery_streak"]
    recovery_duration = state["recovery_duration_ms"]
    recovery_arc = state.get("recovery_arc")

    max_clock = max(row["clock_ms"] for row in rows)
    min_clock = min(row["clock_ms"] for row in rows)

    if failed:
        failure_streak += 1
        if recovery_arc and recovery_arc.get("status") == "active":
            _close_recovery_arc(conn, state, status="broken", result_state=None)
        recovery_streak = 0
        recovery_duration = 0
        recovery_arc = None
        recovery_start = None
        recovery_last = None
    else:
        qualified = score_bp >= policy["healthy_score_bp"]
        if qualified:
            recovery_streak += 1
            failure_streak = 0
            if previous_state == "healthy":
                recovery_start = None
                recovery_last = None
                recovery_duration = 0
                recovery_arc = None
            else:
                recovery_start = state.get("recovery_start_clock_ms")
                if recovery_start is None:
                    recovery_start = min_clock
                    arc_seq = conn.execute("SELECT COUNT(*) AS count FROM recovery_arcs WHERE instance_id=?", (instance_id,)).fetchone()["count"] + 1
                    required_windows, required_duration = _recovery_requirements(previous_state, policy)
                    cursor = conn.execute(
                        """
                        INSERT INTO recovery_arcs(instance_id, arc_seq, status, started_clock_ms, required_windows,
                            required_duration_ms, window_indices_json)
                        VALUES (?, ?, 'active', ?, ?, ?, ?)
                        """,
                        (instance_id, arc_seq, recovery_start, required_windows, required_duration, canonical_json([window_index])),
                    )
                    recovery_arc = {"arc_seq": arc_seq, "status": "active", "window_indices": [window_index]}
                else:
                    if recovery_arc is None:
                        recovery_arc = {"arc_seq": None, "status": "active", "window_indices": []}
                    recovery_arc.setdefault("window_indices", []).append(window_index)
                    if recovery_arc.get("arc_seq") is not None:
                        conn.execute(
                            "UPDATE recovery_arcs SET window_indices_json=? WHERE instance_id=? AND arc_seq=?",
                            (canonical_json(recovery_arc["window_indices"]), instance_id, recovery_arc["arc_seq"]),
                        )
                recovery_last = max_clock
                recovery_duration = max(0, recovery_last - recovery_start)
        else:
            if recovery_arc and recovery_arc.get("status") == "active":
                _close_recovery_arc(conn, state, status="broken", result_state=None)
            recovery_streak = 0
            recovery_duration = 0
            recovery_arc = None
            recovery_start = None
            recovery_last = None

    new_state, reason = adjudicate_state(
        previous_state,
        failed=failed,
        score_bp=score_bp,
        failure_streak=failure_streak,
        recovery_streak=recovery_streak,
        recovery_duration_ms=recovery_duration,
        policy=policy,
    )

    transition_record = None
    if new_state != previous_state:
        if not _legal_transition(previous_state, new_state):
            raise InvalidStateTransition(
                f"illegal transition {previous_state} -> {new_state}",
                location={"instance_id": instance_id},
                details={"window_index": window_index},
            )
        transition_payload = {
            "from_state": previous_state,
            "to_state": new_state,
            "window_index": window_index,
            "at_clock_ms": max_clock,
            "trigger_level": trigger_level,
            "trigger_kind": trigger_kind,
            "trigger_threshold": trigger_threshold,
            "evidence": evidence_by_level,
            "reason": reason,
        }
        transition_seq, prev_hash, entry_hash = _next_transition_hash(conn, instance_id, transition_payload)
        conn.execute(
            """
            INSERT INTO transition_log
            (instance_id, transition_seq, from_state, to_state, window_index, at_clock_ms,
             trigger_level, trigger_kind, trigger_threshold_json, evidence_json, reason,
             prev_hash, entry_hash, patch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id, transition_seq, previous_state, new_state, window_index, max_clock,
                trigger_level, trigger_kind, canonical_json(trigger_threshold), canonical_json(evidence_by_level),
                reason, prev_hash, entry_hash, state.get("updated_patch_id"),
            ),
        )
        transition_record = {"transition_seq": transition_seq, **transition_payload, "entry_hash": entry_hash}
        if new_state == "healthy" and recovery_arc and recovery_arc.get("status") == "active":
            _close_recovery_arc(conn, {**state, "recovery_arc": recovery_arc,
                                        "recovery_last_clock_ms": recovery_last,
                                        "recovery_duration_ms": recovery_duration},
                                status="completed", result_state=new_state)
            recovery_arc = {**recovery_arc, "status": "completed"}
            recovery_start = None

    input_hash = sha256_json([row["content_hash"] for row in rows])
    detail = {
        "level_results": [{k: v for k, v in item.items() if k not in {"weight", "min_score_bp"}} for item in level_results],
        "excluded_thresholds": excluded,
        "policy": policy,
        "reason": reason,
        "transition": transition_record,
    }
    conn.execute(
        """
        INSERT INTO window_summaries
        (instance_id, window_index, window_start_ms, window_end_ms, score_bp, healthy, failed,
         timed_out, trigger_level, trigger_kind, trigger_threshold_json, evidence_json,
         excluded_thresholds_json, threshold_hash, input_hash, detail_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            instance_id, window_index, window_start, window_end, score_bp,
            1 if new_state == "healthy" and not failed else 0, 1 if failed else 0, 1 if timed_out else 0,
            trigger_level, trigger_kind, canonical_json(trigger_threshold), canonical_json(evidence_by_level),
            canonical_json(excluded), pointer["view_hash"], input_hash, canonical_json(detail),
        ),
    )

    for row in rows:
        conn.execute(
            "UPDATE observation_inbox SET status='committed', committed_window_index=? WHERE instance_id=? AND level=? AND seq=?",
            (window_index, instance_id, row["level"], row["seq"]),
        )
        state["committed_seqs"][str(row["level"])] = max(
            int(state["committed_seqs"].get(str(row["level"]), 0)), row["seq"]
        )
        state["committed_stream_seq"] = max(state["committed_stream_seq"], row["stream_seq"])

    state.update(
        {
            "state": new_state,
            "failure_streak": failure_streak,
            "recovery_streak": recovery_streak,
            "recovery_arc": recovery_arc,
            "recovery_start_clock_ms": recovery_start if not failed else None,
            "recovery_last_clock_ms": recovery_last,
            "recovery_duration_ms": recovery_duration,
            "last_window_index": window_index,
            "last_clock_ms": max_clock,
            "threshold_revision": pointer["view_revision"],
            "threshold_hash": pointer["view_hash"],
            "threshold_generation": pointer["threshold_generation"],
            "state_version": state["state_version"] + 1,
        }
    )
    _save_state(conn, state)
    return {
        "instance_id": instance_id,
        "window_index": window_index,
        "score_bp": score_bp,
        "state": new_state,
        "failed": failed,
        "transition": transition_record,
        "excluded_thresholds": excluded,
    }


def _recovery_requirements(state_name: str, policy: dict[str, Any]) -> tuple[int, int]:
    if state_name == "isolated":
        return policy["isolated_recovery_windows"], policy["isolated_recovery_duration_ms"]
    if state_name == "lost":
        return policy["lost_recovery_windows"], policy["lost_recovery_duration_ms"]
    return policy["degraded_recovery_windows"], policy["degraded_recovery_duration_ms"]


def _legal_transition(old: str, new: str) -> bool:
    if old == new:
        return True
    if new == "healthy":
        return old in {"degraded", "lost", "isolated"}
    return STATE_ORDER[new] > STATE_ORDER[old]


def process_instance(store: Store, instance_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    while True:
        with store.transaction(f"instance:{instance_id}") as conn:
            next_row = conn.execute(
                """
                SELECT DISTINCT window_index FROM observation_inbox
                WHERE instance_id=?
                EXCEPT
                SELECT window_index FROM window_summaries WHERE instance_id=?
                ORDER BY window_index LIMIT 1
                """,
                (instance_id, instance_id),
            ).fetchone()
            if next_row is None:
                return results
            window_index = next_row["window_index"]
            result = _commit_window(conn, store, instance_id, window_index)
            fault_point("before_window_commit")
        fault_point("after_window_commit")
        results.append(result)


def process_pending(store: Store, instance_id: str | None = None) -> dict[str, Any]:
    conn = store.read_connection()
    try:
        if instance_id:
            instance_ids = [instance_id]
        else:
            instance_ids = [row["instance_id"] for row in conn.execute("SELECT instance_id FROM instances ORDER BY instance_id")]
    finally:
        conn.close()
    results = []
    for iid in instance_ids:
        results.extend(process_instance(store, iid))
    return {"processed_windows": len(results), "results": results}
