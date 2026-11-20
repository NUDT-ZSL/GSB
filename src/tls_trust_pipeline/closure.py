from __future__ import annotations

import sqlite3
from typing import Any

from .canonical import sha256_json
from .models import Anchor, Snapshot


def _anchor_changed(
    old_anchor: Anchor | None,
    new_by_alias: dict[str, Anchor],
    new_by_fp: dict[str, Anchor],
) -> tuple[bool, str]:
    if old_anchor is None:
        return True, "chain_has_no_resolved_anchor"
    candidate = new_by_alias.get(old_anchor.alias)
    if candidate is None:
        candidate = new_by_fp.get(old_anchor.fingerprint)
    if candidate is None:
        return True, "anchor_removed"
    if candidate.alias != old_anchor.alias:
        return True, "anchor_alias_changed"
    if candidate.semantic_dict() != old_anchor.semantic_dict():
        return True, "anchor_semantics_changed"
    return False, "anchor_unchanged"


def apply_incremental_closure(
    connection: sqlite3.Connection,
    *,
    old_snapshot: Snapshot,
    new_snapshot: Snapshot,
    old_snapshot_id: int,
    new_snapshot_id: int,
    job_id: str,
) -> dict[str, Any]:
    old_anchor_map = old_snapshot.anchor_map()
    new_by_alias = new_snapshot.anchor_map()
    new_by_fp = {anchor.fingerprint: anchor for anchor in new_snapshot.anchors}
    rows = connection.execute(
        """
        SELECT id, hostname, purpose, generation, anchor_alias, anchor_fingerprint,
               chain_json, evidence_json, payload_sha256, request_clock_ms,
               require_revocation, leaf_file, leaf_fingerprint, status, reason_code,
               reason, cache_origin
        FROM chains
        WHERE snapshot_id = ? AND state = 'active'
        ORDER BY hostname, purpose, id
        """,
        (old_snapshot_id,),
    ).fetchall()
    reused_chains = 0
    invalidated_chains = 0
    copied_ids: dict[int, int] = {}
    for row in rows:
        (
            source_id,
            hostname,
            purpose,
            generation,
            anchor_alias,
            anchor_fingerprint,
            chain_json,
            evidence_json,
            payload_sha256,
            request_clock_ms,
            require_revocation,
            leaf_file,
            leaf_fingerprint,
            status,
            reason_code,
            reason,
            _cache_origin,
        ) = row
        old_anchor = old_anchor_map.get(anchor_alias) if anchor_alias else None
        affected, reason_code_anchor = _anchor_changed(old_anchor, new_by_alias, new_by_fp)
        conclusion_key = f"{hostname}|{purpose}|{generation}"
        if affected:
            connection.execute(
                "UPDATE chains SET state = 'stale' WHERE id = ?",
                (source_id,),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO invalidations(
                    from_snapshot_id, to_snapshot_id, from_generation, to_generation,
                    conclusion_kind, conclusion_key, action, reason,
                    source_row_id, target_row_id, source_payload_sha256,
                    target_payload_sha256, job_id
                ) VALUES (?, ?, ?, ?, 'chain', ?, 'invalidate', ?, ?, NULL, ?, NULL, ?)
                """,
                (
                    old_snapshot_id,
                    new_snapshot_id,
                    old_snapshot.generation,
                    new_snapshot.generation,
                    conclusion_key,
                    reason_code_anchor,
                    source_id,
                    payload_sha256,
                    job_id,
                ),
            )
            invalidated_chains += 1
            continue

        connection.execute("UPDATE chains SET state = 'superseded' WHERE id = ?", (source_id,))
        cursor = connection.execute(
            """
            INSERT INTO chains(
                snapshot_id, generation, hostname, purpose, leaf_file, leaf_fingerprint,
                request_clock_ms, require_revocation, status, reason_code, reason,
                anchor_alias, anchor_fingerprint, chain_json, evidence_json,
                payload_sha256, state, reuse_source_id, cache_origin, job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, 'reused', ?)
            """,
            (
                new_snapshot_id,
                new_snapshot.generation,
                hostname,
                purpose,
                leaf_file,
                leaf_fingerprint,
                request_clock_ms,
                require_revocation,
                status,
                reason_code,
                reason,
                anchor_alias,
                anchor_fingerprint,
                chain_json,
                evidence_json,
                payload_sha256,
                source_id,
                job_id,
            ),
        )
        target_id = int(cursor.lastrowid)
        copied_ids[source_id] = target_id
        connection.execute(
            """
            INSERT OR IGNORE INTO invalidations(
                from_snapshot_id, to_snapshot_id, from_generation, to_generation,
                conclusion_kind, conclusion_key, action, reason,
                source_row_id, target_row_id, source_payload_sha256,
                target_payload_sha256, job_id
            ) VALUES (?, ?, ?, ?, 'chain', ?, 'reuse', ?, ?, ?, ?, ?, ?)
            """,
            (
                old_snapshot_id,
                new_snapshot_id,
                old_snapshot.generation,
                new_snapshot.generation,
                conclusion_key,
                "anchor_unchanged",
                source_id,
                target_id,
                payload_sha256,
                payload_sha256,
                job_id,
            ),
        )
        reused_chains += 1

    revocation_rows = connection.execute(
        """
        SELECT id, cert_fp, payload_sha256
        FROM revocations
        WHERE snapshot_id = ? AND state = 'active'
        ORDER BY cert_fp, checked_clock_ms
        """,
        (old_snapshot_id,),
    ).fetchall()
    for source_id, cert_fp, payload_sha256 in revocation_rows:
        connection.execute("UPDATE revocations SET state = 'stale' WHERE id = ?", (source_id,))
        connection.execute(
            """
            INSERT OR IGNORE INTO invalidations(
                from_snapshot_id, to_snapshot_id, from_generation, to_generation,
                conclusion_kind, conclusion_key, action, reason,
                source_row_id, target_row_id, source_payload_sha256,
                target_payload_sha256, job_id
            ) VALUES (?, ?, ?, ?, 'revocation', ?, 'invalidate',
                      'trust_generation_changed', ?, NULL, ?, NULL, ?)
            """,
            (
                old_snapshot_id,
                new_snapshot_id,
                old_snapshot.generation,
                new_snapshot.generation,
                cert_fp,
                source_id,
                payload_sha256,
                job_id,
            ),
        )

    hostname_rows = connection.execute(
        """
        SELECT id, leaf_fingerprint, hostname, purpose, payload_sha256
        FROM hostname_conclusions
        WHERE state = 'active'
        ORDER BY leaf_fingerprint, hostname, purpose
        """
    ).fetchall()
    for source_id, leaf_fp, hostname, purpose, payload_sha256 in hostname_rows:
        key = f"{leaf_fp}|{hostname}|{purpose}"
        connection.execute(
            """
            INSERT OR IGNORE INTO invalidations(
                from_snapshot_id, to_snapshot_id, from_generation, to_generation,
                conclusion_kind, conclusion_key, action, reason,
                source_row_id, target_row_id, source_payload_sha256,
                target_payload_sha256, job_id
            ) VALUES (?, ?, ?, ?, 'hostname', ?, 'reuse', 'certificate_immutable',
                      ?, ?, ?, ?, ?)
            """,
            (
                old_snapshot_id,
                new_snapshot_id,
                old_snapshot.generation,
                new_snapshot.generation,
                key,
                source_id,
                source_id,
                payload_sha256,
                payload_sha256,
                job_id,
            ),
        )

    summary = {
        "chains_reused": reused_chains,
        "chains_invalidated": invalidated_chains,
        "revocations_invalidated": len(revocation_rows),
        "hostname_conclusions_reused": len(hostname_rows),
        "closure_digest": sha256_json(
            {
                "old": old_snapshot.source_digest,
                "new": new_snapshot.source_digest,
                "reused": reused_chains,
                "invalidated": invalidated_chains,
                "revocations": len(revocation_rows),
                "hostnames": len(hostname_rows),
            }
        ),
    }
    return summary

