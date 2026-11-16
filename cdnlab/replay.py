"""Post-restore consistency replay verification.

Re-drives edges and origin with the request/injection sequence recorded in a
snapshot and compares every request's hit status, returned content version
and consistency label against the recorded values. Any divergence (including
config-version or origin-version mismatch) is reported as E_REPLAY with the
first diverging request sequence number.
"""

from __future__ import annotations

from .errors import E_REPLAY

COMPARE_FIELDS = ("outcome", "etag", "last_modified", "entry_version", "stale", "consistency")


def replay_snapshot(orchestrator, snapshot_id):
    """Restore `snapshot_id` on the orchestrator and replay its trace.

    Returns a structured verdict dict; `ok` is True only when every recorded
    request reproduces exactly.
    """
    restore = orchestrator.restore_snapshot(snapshot_id)
    snapshot = restore["snapshot"]
    trace = snapshot["state"]["trace"]

    # Rewind to the trace baseline: empty edge caches, initial origin
    # versions, logical clock at zero. Only then can the recorded sequence
    # be re-driven deterministically.
    from .protocol import send_request
    send_request(orchestrator.origin_port, {
        "op": "restore",
        "contents": snapshot["state"]["initial_origin"]["contents"],
        "versions": snapshot["state"]["initial_origin"]["versions"],
    })
    for port in orchestrator.edge_ports:
        send_request(port, {"op": "reset"})
    orchestrator.tick = 0

    if not restore["config_match"]:
        return {
            "ok": False,
            "code": E_REPLAY,
            "reason": "config_version_mismatch",
            "snapshot_config_version": snapshot["config_version"],
            "current_config_version": orchestrator.policy.config_version,
            "first_diff_seq": None,
            "compared": 0,
            "total": sum(1 for row in trace if row["kind"] == "request"),
        }

    compared = 0
    for row in trace:
        if row["kind"] == "injection":
            _replay_injection(orchestrator, row)
            continue
        orchestrator.tick = row["tick"]
        payload = row["payload"]
        # Re-drive without advancing the clock or double-logging the seq.
        port = orchestrator.edge_ports[int(row["node"].split("-")[1])]
        result = send_request(port, {
            "op": "request", "path": payload["path"], "fields": payload["fields"],
            "tick": row["tick"], "force_validate": payload.get("force_validate", False),
        })
        compared += 1
        recorded = row["result"]
        for field in COMPARE_FIELDS:
            if result.get(field) != recorded.get(field):
                return {
                    "ok": False,
                    "code": E_REPLAY,
                    "reason": "request_divergence",
                    "first_diff_seq": row["seq"],
                    "field": field,
                    "expected": recorded.get(field),
                    "actual": result.get(field),
                    "compared": compared,
                    "total": sum(1 for r in trace if r["kind"] == "request"),
                }
    return {
        "ok": True,
        "compared": compared,
        "total": sum(1 for r in trace if r["kind"] == "request"),
        "first_diff_seq": None,
    }


def _replay_injection(orchestrator, row):
    from .protocol import send_request  # noqa: F811
    payload = row["payload"]
    orchestrator.tick = row["tick"]
    if row["action"] == "origin_update":
        send_request(orchestrator.origin_port,
                     {"op": "update", "path": payload["path"], "content": payload["content"]})
    elif row["action"] == "set_unreachable":
        send_request(orchestrator.origin_port,
                     {"op": "set_reachable", "value": not payload["unreachable"]})
    elif row["action"] == "eviction_pressure":
        pass  # pressure itself is realised by the request rows that follow it
