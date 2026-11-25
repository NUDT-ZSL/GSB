from __future__ import annotations

import os
import signal
import uuid
from typing import Any

from .errors import CapDiagError, ErrorCode
from .jsonutil import dumps
from .storage import applied_v2_generation, write_transaction


def apply_threshold_view(
    conn: Any,
    *,
    unified_view: dict[str, Any],
    candidates_payload: list[dict[str, Any]],
    threshold_version: str,
    primary_snapshot_id: str,
    generation: int,
    effective_logical_ms: int,
    source_version: str = "v2",
    crash_at: str | None = None,
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT threshold_version, generation FROM threshold_snapshots WHERE snapshot_id=?",
        (primary_snapshot_id,),
    ).fetchone()
    current_row = conn.execute(
        "SELECT threshold_version, generation, snapshot_id FROM threshold_current WHERE id=1"
    ).fetchone()
    if existing is not None:
        if current_row is not None and existing["threshold_version"] == current_row["threshold_version"]:
            return {
                "status": "idempotent_already_current",
                "snapshot_id": primary_snapshot_id,
                "threshold_version": existing["threshold_version"],
            }
        if current_row is not None and generation < int(current_row["generation"]):
            raise CapDiagError(
                ErrorCode.VERSION_ROLLBACK,
                "refusing to apply older threshold generation",
                details={
                    "incoming_generation": generation,
                    "current_generation": int(current_row["generation"]),
                    "snapshot_id": primary_snapshot_id,
                },
            )
        return {
            "status": "idempotent_historical_snapshot_not_reactivated",
            "snapshot_id": primary_snapshot_id,
            "threshold_version": existing["threshold_version"],
        }

    if source_version == "v2":
        current_generation = applied_v2_generation(conn)
        if current_generation and generation < current_generation:
            raise CapDiagError(
                ErrorCode.VERSION_ROLLBACK,
                "refusing to apply older threshold generation",
                details={
                    "incoming_generation": generation,
                    "applied_generation": current_generation,
                    "snapshot_id": primary_snapshot_id,
                },
            )

    replacement_id = f"replace-{uuid.uuid4().hex}"
    payload = {
        "unified_view": unified_view,
        "candidates": candidates_payload,
        "threshold_version": threshold_version,
        "primary_snapshot_id": primary_snapshot_id,
        "generation": generation,
        "effective_logical_ms": effective_logical_ms,
    }
    with write_transaction(conn):
        conn.execute(
            """
            INSERT INTO threshold_replacements(
                replacement_id, snapshot_id, threshold_version, generation,
                status, payload_json
            ) VALUES (?, ?, ?, ?, 'staged', ?)
            """,
            (
                replacement_id,
                primary_snapshot_id,
                threshold_version,
                generation,
                dumps(payload),
            ),
        )

    if crash_at == "threshold-stage":
        os.kill(os.getpid(), signal.SIGKILL)

    with write_transaction(conn):
        conn.execute(
            """
            INSERT INTO threshold_snapshots(
                snapshot_id, source_version, generation,
                effective_logical_ms, threshold_version, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            (
                primary_snapshot_id,
                source_version,
                generation,
                effective_logical_ms,
                threshold_version,
                dumps(payload),
            ),
        )
        conn.execute(
            """
            INSERT INTO threshold_current(id, snapshot_id, threshold_version, generation, payload_json)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                snapshot_id=excluded.snapshot_id,
                threshold_version=excluded.threshold_version,
                generation=excluded.generation,
                payload_json=excluded.payload_json
            """,
            (
                primary_snapshot_id,
                threshold_version,
                generation,
                dumps(payload),
            ),
        )
        conn.execute(
            "UPDATE threshold_replacements SET status='committed', threshold_version=? WHERE replacement_id=?",
            (threshold_version, replacement_id),
        )

    return {
        "status": "applied",
        "replacement_id": replacement_id,
        "snapshot_id": primary_snapshot_id,
        "threshold_version": threshold_version,
        "generation": generation,
    }


def candidates_from_current_payload(payload: dict[str, Any]) -> list[Any]:
    from decimal import Decimal

    from .thresholds import ThresholdCandidate

    candidates = []
    for item in payload.get("candidates", []):
        values = dict(item)
        values["value"] = Decimal(str(values["value"]))
        candidates.append(ThresholdCandidate(**values))
    return candidates
