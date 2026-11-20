from __future__ import annotations

import json
from typing import Any

from .canonical import sha256_json
from .errors import E_AUDIT_FAILED, PipelineError
from .store import Store


def _fail(code: str, message: str, **details: Any) -> None:
    raise PipelineError(E_AUDIT_FAILED, message, {"check": code, **details})


def verify_audit(store: Store) -> dict[str, Any]:
    checks: list[str] = []
    with store.read() as connection:
        current = store.current_snapshot(connection)
        if current is None:
            _fail("current_snapshot", "no current snapshot")
        current_row = connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_version = ?",
            (current.snapshot_version,),
        ).fetchone()
        if current_row is None:
            _fail("current_snapshot", "current snapshot row is missing")
        anchor_rows = connection.execute(
            "SELECT * FROM snapshot_anchors WHERE snapshot_id = ? ORDER BY alias",
            (current_row["id"],),
        ).fetchall()
        if len(anchor_rows) != current_row["anchor_count"]:
            _fail("anchor_count", "snapshot anchor count does not match staged rows")
        if len({row["alias"] for row in anchor_rows}) != len(anchor_rows):
            _fail("anchor_alias_unique", "snapshot aliases are not unique")
        if len({row["fingerprint"] for row in anchor_rows}) != len(anchor_rows):
            _fail("anchor_fp_unique", "snapshot fingerprints are not unique")
        for row in anchor_rows:
            if row["revoked"] == 1 and row["can_issue_intermediates"] == 1:
                _fail(
                    "anchor_tombstone_semantics",
                    "tombstoned anchor can issue intermediates",
                    alias=row["alias"],
                )
        checks.append("anchor_count_and_uniqueness")
        checks.append("anchor_tombstone_semantics")

        chain_rows = connection.execute(
            "SELECT * FROM chains WHERE generation = ? ORDER BY hostname, purpose, id",
            (current.generation,),
        ).fetchall()
        active_keys: set[str] = set()
        for row in chain_rows:
            if row["state"] != "active":
                continue
            key = f"{row['hostname']}|{row['purpose']}|{row['generation']}"
            if key in active_keys:
                _fail("chain_unique", "duplicate active chain conclusion", key=key)
            active_keys.add(key)
            if row["snapshot_id"] != current_row["id"]:
                _fail("chain_snapshot", "active chain points at a non-current snapshot", key=key)
            conclusion = json.loads(row["evidence_json"])
            if sha256_json(conclusion) != row["payload_sha256"]:
                _fail("chain_payload", "chain payload hash mismatch", key=key)
            if conclusion.get("hostname") != row["hostname"] or conclusion.get(
                "purpose"
            ) != row["purpose"]:
                _fail("chain_key", "chain row key differs from evidence", key=key)
            chain_json = json.loads(row["chain_json"])
            if chain_json != conclusion.get("chain"):
                _fail("chain_json", "chain list differs from evidence", key=key)
            if row["cache_origin"] == "reused" and row["reuse_source_id"] is None:
                _fail("chain_reuse", "reused chain has no source", key=key)
        checks.append("active_chain_conclusions")

        revocation_rows = connection.execute(
            "SELECT * FROM revocations WHERE generation = ? ORDER BY cert_fp",
            (current.generation,),
        ).fetchall()
        for row in revocation_rows:
            if row["state"] != "active":
                continue
            if row["snapshot_id"] != current_row["id"]:
                _fail("revocation_snapshot", "active revocation points at another snapshot")
            reconstructed = {
                "certificate_fingerprint": row["cert_fp"],
                "serial_hex": row["serial_hex"],
                "status": row["status"],
                "reason": row["reason"],
                "source": row["source"],
                "evidence_sha256": row["evidence_sha256"],
                "record_sha256": row["record_sha256"],
                "checked_clock_ms": row["checked_clock_ms"],
                "revoked_at_ms": row["revoked_at_ms"],
            }
            if sha256_json(reconstructed) != row["payload_sha256"]:
                _fail(
                    "revocation_payload",
                    "revocation payload hash mismatch",
                    cert_fp=row["cert_fp"],
                )
        checks.append("active_revocation_conclusions")

        hostname_rows = connection.execute(
            "SELECT * FROM hostname_conclusions ORDER BY leaf_fingerprint, hostname, purpose"
        ).fetchall()
        for row in hostname_rows:
            if row["state"] != "active":
                continue
            try:
                evidence = json.loads(row["san_json"])
            except json.JSONDecodeError:
                _fail("hostname_payload", "hostname evidence is not valid JSON")
            if sha256_json(evidence) != row["payload_sha256"]:
                _fail(
                    "hostname_payload",
                    "hostname payload hash mismatch",
                    hostname=row["hostname"],
                )
        checks.append("hostname_conclusions")

        invalidation_rows = connection.execute(
            "SELECT * FROM invalidations ORDER BY id"
        ).fetchall()
        source_tables = {
            "chain": "chains",
            "revocation": "revocations",
            "hostname": "hostname_conclusions",
        }
        for row in invalidation_rows:
            table = source_tables.get(row["conclusion_kind"])
            if table is None:
                _fail("invalidation_kind", "unknown invalidation kind")
            source = connection.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row["source_row_id"],)
            ).fetchone()
            if source is None:
                _fail(
                    "invalidation_source",
                    "invalidation source row is missing",
                    id=row["id"],
                )
            if row["action"] == "invalidate":
                if source["state"] == "active":
                    _fail(
                        "invalidation_state",
                        "invalidated conclusion is still active",
                        id=row["id"],
                    )
                if row["target_row_id"] is not None:
                    _fail("invalidation_target", "invalidate action has a target row")
                if source["payload_sha256"] != row["source_payload_sha256"]:
                    _fail(
                        "invalidation_source_payload",
                        "invalidation source payload differs",
                        id=row["id"],
                    )
            elif row["action"] == "reuse":
                if source["payload_sha256"] != row["source_payload_sha256"]:
                    _fail(
                        "reuse_source_payload",
                        "reuse source payload differs",
                        id=row["id"],
                    )
                if row["source_payload_sha256"] != row["target_payload_sha256"]:
                    _fail(
                        "reuse_payload",
                        "reuse changed the conclusion payload",
                        id=row["id"],
                    )
                if row["target_row_id"] is None:
                    _fail("reuse_target", "reuse action has no target row")
                target = connection.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (row["target_row_id"],)
                ).fetchone()
                if target is None:
                    _fail("reuse_target", "reuse target row is missing", id=row["id"])
                if target["payload_sha256"] != row["target_payload_sha256"]:
                    _fail(
                        "reuse_target_payload",
                        "reuse target payload differs",
                        id=row["id"],
                    )
                if row["conclusion_kind"] == "chain" and (
                    target["state"] != "active"
                    or target["generation"] != row["to_generation"]
                ):
                    _fail(
                        "reuse_target_state",
                        "reused chain target is not active in target generation",
                        id=row["id"],
                    )
                if row["conclusion_kind"] == "hostname" and target["state"] != "active":
                    _fail(
                        "reuse_target_state",
                        "reused hostname conclusion is not active",
                        id=row["id"],
                    )
                if row["conclusion_kind"] == "revocation":
                    _fail(
                        "revocation_reuse_forbidden",
                        "revocation conclusions must be re-adjudicated by generation",
                    )
            else:
                _fail("invalidation_action", "unknown invalidation action")
        checks.append("invalidation_consistency")

        running = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'"
        ).fetchone()["count"]
        stale_active = connection.execute(
            "SELECT COUNT(*) AS count FROM chains WHERE state = 'stale' AND generation = ?",
            (current.generation,),
        ).fetchone()["count"]
        if stale_active:
            _fail("stale_current_generation", "stale chain exists in current generation")
        summary = {
            "ok": True,
            "current_version": current.snapshot_version,
            "current_generation": current.generation,
            "anchor_count": len(anchor_rows),
            "active_chains": len(active_keys),
            "active_revocations": len(revocation_rows),
            "hostname_conclusions": len(hostname_rows),
            "invalidations": len(invalidation_rows),
            "running_jobs": running,
            "checks": checks,
        }
        summary["audit_digest"] = sha256_json(summary)
        return summary

