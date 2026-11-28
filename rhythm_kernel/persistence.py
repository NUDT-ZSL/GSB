"""Snapshot persistence with per-result integrity hashes.

Snapshots are self-contained: they embed the project view that produced the
results so a restarted process can verify that untouched results still match
the saved per-event SHA-256 digests before continuing with new events.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .model import SNAPSHOT_VERSION, clone, sha256_hex


def _result_map(results):
    return {result["event_id"]: clone(result) for result in results}


def build_snapshot(report, project):
    """Assemble the snapshot document and its integrity block."""
    result_map = _result_map(report["results"])
    integrity = {
        "consistency_sha256": sha256_hex(report["consistency_report"]),
        "project_sha256": sha256_hex(project),
        "result_sha256": {
            event_id: sha256_hex(entry) for event_id, entry in result_map.items()
        },
        "stats_sha256": sha256_hex(report["stats"]),
        "trigger_sha256": sha256_hex(report["trigger_sequence"]),
        "validation_sha256": sha256_hex(report["validation"]),
    }
    return {
        "algorithm_version": report["algorithm_version"],
        "consistency_report": clone(report["consistency_report"]),
        "incremental_history": clone(report["incremental_history"]),
        "integrity": integrity,
        "project": clone(project),
        "results": result_map,
        "saved_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_version": SNAPSHOT_VERSION,
        "state_revision": report["state_revision"],
        "stats": clone(report["stats"]),
        "trigger_sequence": clone(report["trigger_sequence"]),
        "validation": clone(report["validation"]),
    }


def save_snapshot(path, report, project):
    snapshot = build_snapshot(report, project)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return snapshot


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    verify_snapshot(snapshot)
    return snapshot


def verify_snapshot(snapshot):
    """Recompute every digest and raise on the first tampered section."""
    integrity = snapshot["integrity"]
    checks = {
        "project_sha256": sha256_hex(snapshot["project"]),
        "stats_sha256": sha256_hex(snapshot["stats"]),
        "trigger_sha256": sha256_hex(snapshot["trigger_sequence"]),
        "validation_sha256": sha256_hex(snapshot["validation"]),
        "consistency_sha256": sha256_hex(snapshot["consistency_report"]),
    }
    for name, digest in checks.items():
        if digest != integrity[name]:
            raise ValueError(f"snapshot integrity check failed: {name}")
    for event_id, entry in snapshot["results"].items():
        digest = sha256_hex(entry)
        if digest != integrity["result_sha256"][event_id]:
            raise ValueError(
                f"snapshot per-event integrity check failed: {event_id}"
            )
    return True
