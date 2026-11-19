"""Post-crash recovery adjudication.

Recovery trusts only persisted state: pending intents are half-committed
actions (dispatched, maybe applied remotely, but never committed locally)
and are re-dispatched idempotently before being committed; plan actions
with neither watermark nor intent were never started and are replayed
through the normal commit path. Committed watermarks are never re-done,
so already-counted traffic ladder rungs stay untouched.
"""

from __future__ import annotations

import json

from .executor import Orchestrator
from .models import UnifiedPlan, digest_of
from .store import Store


def recover(store: Store, plan: UnifiedPlan, plan_version: int,
            orchestrator: Orchestrator, clock_ms: int) -> dict:
    conn = store.connect()
    try:
        pending = conn.execute(
            "SELECT service, action_id, action_version, payload_json"
            " FROM action_intent"
            " WHERE plan_version = ? AND status = 'pending'"
            " ORDER BY service, action_id",
            (plan_version,)).fetchall()
    finally:
        conn.close()

    replayed_half_committed = []
    for row in pending:
        payload = json.loads(row["payload_json"])
        orchestrator._dispatch_action(payload["service"],
                                      payload["action_id"],
                                      payload["version"], payload["op"])
        conn = store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO action_watermark"
                " (plan_version, service, action_id, action_version,"
                "  content_digest, clock_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (plan_version, row["service"], row["action_id"],
                 row["action_version"], digest_of(payload), clock_ms))
            conn.execute(
                "UPDATE action_intent SET status = 'committed'"
                " WHERE plan_version = ? AND service = ? AND action_id = ?",
                (plan_version, row["service"], row["action_id"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        replayed_half_committed.append(
            {"service": row["service"], "action_id": row["action_id"]})

    conn = store.connect()
    try:
        done = conn.execute(
            "SELECT service, action_id FROM action_watermark"
            " WHERE plan_version = ?", (plan_version,)).fetchall()
        started = conn.execute(
            "SELECT DISTINCT service FROM decision_log"
            " WHERE plan_version = ? AND decision = 'FAILOVER_START'",
            (plan_version,)).fetchall()
    finally:
        conn.close()
    committed = {(r["service"], r["action_id"]) for r in done}
    in_flight = {r["service"] for r in started}

    replayed_missing = []
    for service in sorted(plan.services):
        if service not in in_flight:
            continue  # never started: left to the normal failover run
        sp = plan.services[service]
        for action in sp.actions:
            if (service, action.action_id) in committed:
                continue
            orchestrator._commit_action(plan_version, service,
                                        {"action_id": action.action_id,
                                         "op": action.op,
                                         "generation": action.generation},
                                        clock_ms)
            replayed_missing.append(
                {"service": service, "action_id": action.action_id})

    return {
        "half_committed_replayed": replayed_half_committed,
        "missing_replayed": replayed_missing,
        "already_committed": [
            {"service": s, "action_id": a} for s, a in sorted(committed)],
    }
