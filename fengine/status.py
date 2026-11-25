"""Stable JSON status snapshot of operator states and ledger summaries."""
import json


def build_status(store) -> dict:
    version = store.current_version()
    rows = store.conn.execute(
        "SELECT node_id, window_id, layer, status, attempt, memory_bytes,"
        " abandon_reason, reused, ledger_json FROM op_units"
        " WHERE graph_version=? ORDER BY node_id, window_id",
        (version,)).fetchall()
    nodes = {}
    counts = {"pending": 0, "running": 0, "done": 0, "abandoned": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        n = nodes.setdefault(r["node_id"], {
            "layer": r["layer"],
            "done": 0, "pending": 0, "running": 0, "abandoned": 0,
            "reused": 0, "attempts": 0,
            "abandon_reasons": [],
            "error_bound_total": 0.0,
            "ledger_entries": 0,
        })
        n[r["status"]] += 1
        n["attempts"] += r["attempt"]
        if r["reused"]:
            n["reused"] += 1
        if r["abandon_reason"]:
            n["abandon_reasons"].append(r["abandon_reason"])
        if r["ledger_json"]:
            led = json.loads(r["ledger_json"])
            n["error_bound_total"] += led["accumulated_error_bound"]
            n["ledger_entries"] += len(led["entries"])
    for n in nodes.values():
        n["abandon_reasons"] = sorted(set(n["abandon_reasons"]))
        n["error_bound_total"] = round(n["error_bound_total"], 12)
    commits = store.conn.execute(
        "SELECT COUNT(*) AS n FROM window_commits WHERE graph_version=?",
        (version,)).fetchone()["n"]
    return {
        "graph_version": version,
        "logical_clock": store.get_clock(),
        "plan_summary": store.get_meta("plan_summary"),
        "memory_balance_bytes": store.memory_balance(),
        "unit_counts": counts,
        "windows_committed": commits,
        "nodes": nodes,
    }


def status_json(store) -> str:
    return json.dumps(build_status(store), sort_keys=True, indent=2)
