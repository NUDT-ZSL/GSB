from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .engine import claim_next_attempt, finalize_claimed_attempt
from .errors import Code, KvError
from .ledger import encode_frame, read_frame, write_frame_at
from .storage import Database, nvme_file_lock
from .util import deterministic_payload, require_clock_ms


def _repair_writing_attempt(db: Database, row: Any, clock: int) -> Dict[str, Any]:
    claim = dict(row)
    uid = claim["uid"]
    nvme_path = Path(claim["file_path"])
    _, lock_path = db.paths()
    offset = int(claim["file_offset"])
    expected_length = int(claim["frame_length"])

    verified = read_frame(nvme_path, offset, expected_length)
    action = "verified_complete_frame"
    if not verified.get("valid"):
        payload = deterministic_payload(claim["entry_id"], int(claim["actual_bytes"]))
        import json

        header = json.loads(claim["header_json"])
        frame = encode_frame(header, payload)
        if len(frame) != expected_length:
            raise KvError(Code.INVARIANT_VIOLATION, f"recovery frame length mismatch for {uid}")
        write_frame_at(nvme_path, lock_path, offset, frame, already_locked=True)
        rechecked = read_frame(nvme_path, offset, expected_length)
        if not rechecked.get("valid"):
            raise KvError(Code.INVARIANT_VIOLATION, f"recovery rewrite invalid for {uid}: {rechecked.get('reason')}")
        action = f"rewrote_{verified.get('reason', 'invalid_frame')}"

    record = finalize_claimed_attempt(db, claim, clock, already_locked=True)
    return {"uid": uid, "entry_id": claim["entry_id"], "offset": offset, "action": action, "record": record}


def recover_workspace(db: Database, clock_ms: int) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    config = db.require_config()
    nvme_path, lock_path = db.paths()

    writing_rows = db.conn.execute(
        """
        SELECT * FROM eviction_attempts
         WHERE status='writing'
         ORDER BY layer_id, file_offset, attempt_id
        """
    ).fetchall()
    planned_rounds = db.conn.execute(
        """
        SELECT DISTINCT round_id FROM eviction_attempts
         WHERE status='planned'
         ORDER BY round_id
        """
    ).fetchall()

    repaired: List[Dict[str, Any]] = []
    replayed: List[Dict[str, Any]] = []
    with nvme_file_lock(lock_path):
        for row in writing_rows:
            repaired.append(_repair_writing_attempt(db, row, clock))

        for round_row in planned_rounds:
            round_id = round_row["round_id"]
            while True:
                claim = claim_next_attempt(db, round_id, "recovery", clock)
                if claim is None:
                    break
                record = finalize_claimed_attempt(db, claim, clock, already_locked=True)
                replayed.append(
                    {
                        "uid": claim["uid"],
                        "round_id": round_id,
                        "entry_id": claim["entry_id"],
                        "offset": claim["file_offset"],
                        "action": "started_planned_attempt",
                        "fixed_value": record["fixed_value"],
                    }
                )

    with db.immediate() as conn:
        conn.execute(
            """
            UPDATE rounds
               SET status='recovered_completed', closed_clock=?
             WHERE status='open'
               AND NOT EXISTS (
                    SELECT 1 FROM eviction_attempts a
                     WHERE a.round_id=rounds.round_id
                       AND a.status IN ('planned','writing')
               )
            """,
            (clock,),
        )
        db.set_meta("logical_clock_watermark_ms", str(max(int(db.get_meta("logical_clock_watermark_ms", "0")), clock)))
        active_policy = db.get_meta("active_policy_version")

    remaining = db.conn.execute(
        "SELECT COUNT(*) n FROM eviction_attempts WHERE status IN ('planned','writing')"
    ).fetchone()["n"]
    verdict = "clean" if not repaired and not replayed else "replayed_unfinished"
    if remaining:
        verdict = "still_unfinished"
    return {
        "command": "resume",
        "ok": remaining == 0,
        "recovery_verdict": verdict,
        "remaining_unfinished": remaining,
        "repaired_writing": repaired,
        "replayed_planned": replayed,
        "active_policy_version": active_policy,
        "nvme_file": str(nvme_path),
    }
