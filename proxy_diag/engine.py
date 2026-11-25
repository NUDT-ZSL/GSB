"""Loopback HTTP replay and persistent forwarding adjudication."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .errors import E_HTTP_REPLAY, E_LOCK, E_STATE, WorkbenchError
from .locks import FileLock, session_lock, session_lock_path
from .statemachine import build_transitions
from .store import Store


def _session_payload(store: Store, attempt: Any) -> dict[str, Any]:
    row = store.connection.execute(
        "SELECT payload_json FROM snapshot_session WHERE snapshot_version=? AND connection_id=?",
        (attempt["snapshot_version"], attempt["connection_id"]),
    ).fetchone()
    if row is not None:
        return json.loads(row["payload_json"])
    view = store.get_unified_view()
    if view:
        for session in view["sessions"]:
            if session["connection_id"] == attempt["connection_id"]:
                return session
    raise WorkbenchError(E_STATE, "session snapshot payload is missing", {"connection_id": attempt["connection_id"]})


def _table_entries(store: Store, table_version: str) -> tuple[list[dict[str, Any]], int | None]:
    row = store.connection.execute("SELECT * FROM table_version WHERE version=?", (table_version,)).fetchone()
    if row is None:
        return [], None
    return json.loads(row["entries_json"]), int(row["created_clock_ms"])


def _http_call(base_url: str, connection_id: str, hop: int, headers: dict[str, str], timeout: float = 10.0) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/mock/{urllib.parse.quote(connection_id, safe='')}/{hop}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbenchError(E_HTTP_REPLAY, "loopback service returned invalid JSON", {"url_path": f"/mock/{connection_id}/{hop}"}) from exc
    return {
        "request_method": "GET",
        "request_path": f"/mock/{connection_id}/{hop}",
        "request_headers": headers,
        "response_status_code": int(status),
        "response_digest": payload.get("digest"),
        "response_body_status_code": payload.get("status_code"),
    }


def execute_attempt(
    store: Store,
    attempt: Any,
    *,
    base_url: str,
    plan: dict[str, Any],
    replay_clock_ms: int,
) -> dict[str, Any]:
    connection_id = attempt["connection_id"]
    table_version = attempt["table_version"]
    session = _session_payload(store, attempt)
    entries, table_clock = _table_entries(store, table_version)
    entries_by_route = {entry["route_key"]: entry for entry in entries}
    route_keys = session.get("route_keys", [])
    tombstoned = any(bool(entries_by_route.get(route, {}).get("tombstone")) for route in route_keys)

    if tombstoned:
        transitions = build_transitions(
            session,
            tombstoned=True,
            table_version_created_clock=table_clock,
            replay_clock_ms=replay_clock_ms,
        )
        result = {
            "connection_id": connection_id,
            "conclusion": "skipped",
            "skip_reason": "forward_table_tombstone",
            "snapshot_version": attempt["snapshot_version"],
            "generation": attempt["generation"],
            "table_version": table_version,
            "route_keys": route_keys,
            "http_calls": [],
            "call_count": 0,
            "state_machine": transitions,
            "table_version_comparison": session.get("table_version_comparison"),
            "judgement_basis": {
                "tombstoned_routes": [route for route in route_keys if entries_by_route.get(route, {}).get("tombstone")],
                "replay_clock_ms": replay_clock_ms,
                "test_service": "http://127.0.0.1/loopback",
            },
        }
        store.commit_attempt(attempt["attempt_id"], attempt["claim_token"], "skipped", result, 0, replay_clock_ms)
        return result

    hops = sorted({int(record["hop"]) for record in session.get("records", [])})
    plan_calls = plan.get("calls", {})
    hop_results: list[dict[str, Any]] = []
    call_count = 0
    for hop in hops:
        record_headers: dict[str, str] = {}
        for record in session.get("records", []):
            if int(record["hop"]) == hop and record.get("request_headers"):
                record_headers = dict(record["request_headers"])
                break
        logical_clock = record_headers.get("X-Logical-Clock") or str(session.get("declared_logical_clock_ms"))
        headers = {
            "Accept": "application/json",
            "X-Connection-Id": connection_id,
            "X-Hop": str(hop),
            "X-Logical-Clock": str(logical_clock),
            "X-Route-Key": str(route_keys[0] if route_keys else ""),
            "X-Seed": "434-fixed",
            "X-Table-Version": table_version,
        }
        observed = _http_call(base_url, connection_id, hop, headers)
        call_count += 1
        expected = (((plan_calls.get(connection_id) or {}).get("hops") or {}).get(str(hop)) or {})
        expected_status = expected.get("status")
        expected_digest = expected.get("digest")
        status_match = expected_status is not None and observed["response_status_code"] == int(expected_status)
        digest_match = expected_digest is not None and observed["response_digest"] == expected_digest
        hop_success = 200 <= observed["response_status_code"] < 400 and status_match and digest_match
        hop_results.append(
            {
                "hop": hop,
                **observed,
                "expected_status_code": expected_status,
                "expected_digest": expected_digest,
                "status_match": status_match,
                "digest_match": digest_match,
                "hop_success": hop_success,
            }
        )

    all_match = all(item["status_match"] and item["digest_match"] for item in hop_results)
    any_success_status = any(200 <= item["response_status_code"] < 400 for item in hop_results)
    all_success_status = all(200 <= item["response_status_code"] < 400 for item in hop_results)
    if all_match and all_success_status:
        conclusion = "success"
        reason = "all_hops_returned_expected_2xx_and_digest"
    else:
        conclusion = "failed"
        if not all_match:
            reason = "deterministic_status_or_digest_mismatch"
        elif not all_success_status:
            reason = "upstream_returned_non_success_status"
        else:
            reason = "replay_failed"
    transitions = build_transitions(
        session,
        tombstoned=False,
        table_version_created_clock=table_clock,
        replay_clock_ms=replay_clock_ms,
        outcome=conclusion,
    )
    result = {
        "connection_id": connection_id,
        "conclusion": conclusion,
        "failure_reason": None if conclusion == "success" else reason,
        "snapshot_version": attempt["snapshot_version"],
        "generation": attempt["generation"],
        "table_version": table_version,
        "route_keys": route_keys,
        "http_calls": hop_results,
        "call_count": call_count,
        "state_machine": transitions,
        "table_version_comparison": session.get("table_version_comparison"),
        "judgement_basis": {
            "all_fixtures_matched": all_match,
            "any_success_status": any_success_status,
            "all_success_status": all_success_status,
            "replay_clock_ms": replay_clock_ms,
            "test_service": "http://127.0.0.1/loopback",
            "preserved_header_names": sorted(headers.keys()),
        },
    }
    store.commit_attempt(attempt["attempt_id"], attempt["claim_token"], conclusion, result, call_count, replay_clock_ms)
    return result


def replay_targeted(
    store: Store,
    paths,
    connection_id: str,
    *,
    base_url: str,
    plan: dict[str, Any],
    replay_clock_ms: int,
    worker_id: str = "cli",
) -> dict[str, Any]:
    from .locks import session_lock

    with session_lock(paths.session_lock_dir, connection_id, blocking=True):
        rows = store.pending_attempts(connection_id)
        if not rows:
            committed = store.committed_attempt(connection_id)
            return {
                "connection_id": connection_id,
                "action": "already_committed",
                "conclusion": committed["conclusion"] if committed else "missing",
                "evidence_hash": committed["evidence_hash"] if committed else None,
            }
        row = rows[0]
        token = secrets.token_hex(12)
        claimed = store.claim_attempt(row["attempt_id"], worker_id, token)
        if claimed is None:
            return {"connection_id": connection_id, "action": "already_committed"}
        result = execute_attempt(
            store,
            claimed,
            base_url=base_url,
            plan=plan,
            replay_clock_ms=replay_clock_ms,
        )
        return {"connection_id": connection_id, "action": "replayed", "conclusion": result["conclusion"], "result": result}


def replay_all(
    store: Store,
    paths,
    *,
    base_url: str,
    plan: dict[str, Any],
    replay_clock_ms: int,
    worker_id: str = "cli",
    limit: int | None = None,
) -> dict[str, Any]:
    processed: list[dict[str, Any]] = []
    skipped_locked: list[str] = []
    while True:
        rows = store.pending_attempts(limit=limit or 200)
        if not rows:
            break
        progressed = False
        for row in rows:
            connection_id = row["connection_id"]
            try:
                lock = FileLock(session_lock_path(paths.session_lock_dir, connection_id)).acquire(blocking=False)
            except WorkbenchError as exc:
                if exc.code != E_LOCK:
                    raise
                skipped_locked.append(connection_id)
                continue
            try:
                fresh = store.connection.execute(
                    "SELECT * FROM forwarding_attempt WHERE attempt_id=?",
                    (row["attempt_id"],),
                ).fetchone()
                if fresh is None or fresh["status"] != "pending":
                    continue
                token = secrets.token_hex(12)
                claimed = store.claim_attempt(fresh["attempt_id"], worker_id, token)
                if claimed is None:
                    continue
                result = execute_attempt(
                    store,
                    claimed,
                    base_url=base_url,
                    plan=plan,
                    replay_clock_ms=replay_clock_ms,
                )
                processed.append(
                    {
                        "connection_id": connection_id,
                        "action": "replayed",
                        "conclusion": result["conclusion"],
                        "attempt_id": fresh["attempt_id"],
                    }
                )
                progressed = True
            finally:
                lock.release()
        if not progressed:
            break
    return {"ok": True, "worker_id": worker_id, "processed": processed, "skipped_locked": sorted(set(skipped_locked))}


def recover_claims(store: Store, paths) -> dict[str, Any]:
    rows = store.connection.execute("SELECT * FROM forwarding_attempt WHERE status='claimed' ORDER BY attempt_id").fetchall()
    reset: list[str] = []
    active_locks: list[str] = []
    for row in rows:
        connection_id = row["connection_id"]
        try:
            with session_lock(paths.session_lock_dir, connection_id, blocking=False):
                store.reset_claim(row["attempt_id"])
                reset.append(connection_id)
        except WorkbenchError as exc:
            if exc.code != E_LOCK:
                raise
            active_locks.append(connection_id)
    return {"reset": sorted(reset), "active_locks": sorted(active_locks)}
