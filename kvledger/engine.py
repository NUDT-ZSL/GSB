from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import Code, KvError
from .ledger import add_conversion, encode_frame, expected_frame_length, read_frame, write_frame_at
from .policy import LoadedPolicyPackage, load_and_merge_package
from .storage import Database, dumps_json, nvme_file_lock
from .util import canonical_hash, deterministic_payload, require_clock_ms, stable_json_dump
from .validation import (
    MemoryConfig,
    RequestSpec,
    validate_memory_config_file,
    validate_requests_file,
    validate_requests_payload,
    validate_memory_config_payload,
    read_json,
)
from .util import sha256_bytes


def _policy_for_layers(conn: sqlite3.Connection, version: str) -> Dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM policy_entries WHERE policy_version=?",
        (version,),
    ).fetchall()
    return {row["layer_id"]: row for row in rows}


def _target_capacity(capacity: int, policy_row: Optional[sqlite3.Row]) -> int:
    if policy_row is None:
        return capacity
    from decimal import Decimal

    threshold = Decimal(policy_row["threshold_text"])
    target = int((Decimal(capacity) * threshold).to_integral_value(rounding="ROUND_FLOOR"))
    return max(1, min(capacity, target))


def advance_next_token(db: Database, seq_id: str, clock_ms: int) -> Optional[int]:
    clock = require_clock_ms(clock_ms)
    config = db.require_config()
    scale = int(config["scale"])

    with db.immediate() as conn:
        request = conn.execute("SELECT * FROM requests WHERE seq_id=?", (seq_id,)).fetchone()
        if request is None:
            raise KvError(Code.STATE_NOT_INITIALIZED, f"unknown seq_id {seq_id}")
        if int(request["watermark_token"]) >= int(request["generated_tokens"]):
            return None

        token = int(request["watermark_token"]) + 1
        raw = json.loads(request["payload_json"])
        layer_specs = {item["layer_id"]: item for item in raw["layers"]}

        for layer_id in sorted(layer_specs):
            layer_spec = layer_specs[layer_id]
            if "entry_bytes" in layer_spec and layer_spec["entry_bytes"] is not None:
                size = int(layer_spec["entry_bytes"][token - 1])
            else:
                size = int(layer_spec.get("kv_entry_bytes", layer_spec.get("byte_size")))
            entry_id = f"{seq_id}:{layer_id}:t{token:05d}"
            payload_digest = sha256_bytes(deterministic_payload(entry_id, size))
            conn.execute(
                """
                INSERT INTO kv_entries(
                    entry_id, seq_id, layer_id, token_index, size_bytes,
                    digest, state, created_clock
                ) VALUES (?, ?, ?, ?, ?, ?, 'hbm', ?)
                """,
                (entry_id, seq_id, layer_id, token, size, payload_digest, clock),
            )

            layer_row = conn.execute(
                "SELECT * FROM request_layers WHERE seq_id=? AND layer_id=?",
                (seq_id, layer_id),
            ).fetchone()
            carry_in = int(layer_row["carry_bytes"])
            conversion = add_conversion(
                conn,
                scope_type="kv_entry",
                scope_key=entry_id,
                seq_id=seq_id,
                layer_id=layer_id,
                source_value_text=str(size),
                scale=scale,
                clock_ms=clock,
                value=size,
                carry_in=carry_in,
                detail={"token_index": token},
            )
            conn.execute(
                """
                UPDATE request_layers
                   SET total_bytes = total_bytes + ?,
                       hbm_used_bytes = hbm_used_bytes + ?,
                       fixed_units_cumulative = fixed_units_cumulative + ?,
                       carry_bytes = ?,
                       error_bound_bytes = error_bound_bytes + ?
                 WHERE seq_id=? AND layer_id=?
                """,
                (
                    size,
                    size,
                    int(conversion["fixed_delta"]),
                    int(conversion["carry_out"]),
                    int(conversion["error_bound_bytes"]),
                    seq_id,
                    layer_id,
                ),
            )
            conn.execute(
                "UPDATE layer_state SET hbm_used_bytes = hbm_used_bytes + ? WHERE layer_id=?",
                (size, layer_id),
            )

        new_status = "generated" if token >= int(request["generated_tokens"]) else "streaming"
        update_cursor = conn.execute(
            """
            UPDATE requests
               SET watermark_token=?, status=?, updated_clock=?
             WHERE seq_id=? AND watermark_token=?
            """,
            (token, new_status, clock, seq_id, token - 1),
        )
        if update_cursor.rowcount != 1:
            raise KvError(Code.INVARIANT_VIOLATION, f"watermark update lost for {seq_id}")
        _clock_watermark(db, clock)
        return token


def _round_row(db: Database, round_id: str) -> sqlite3.Row:
    row = db.conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    if row is None:
        raise KvError(Code.STATE_NOT_INITIALIZED, f"round does not exist: {round_id}")
    return row


def create_round(db: Database, round_id: str, clock_ms: int) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    config = db.require_config()
    policy_version = db.require_active_policy()
    with db.immediate() as conn:
        existing = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if existing is not None:
            return {"created": False, "round_id": round_id, "status": existing["status"]}
        conn.execute(
            """
            INSERT INTO rounds(
                round_id, status, clock_ms, budget_bytes, max_concurrent,
                policy_version, created_clock
            ) VALUES (?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                round_id,
                clock,
                int(config["budget_bytes"]),
                int(config["max_concurrent"]),
                policy_version,
                clock,
            ),
        )
        conn.execute("UPDATE requests SET bound_policy_version=?", (policy_version,))
        _clock_watermark(db, clock)
    return {"created": True, "round_id": round_id, "status": "open"}


def plan_pressure_candidates(
    db: Database,
    round_id: str,
    clock_ms: int,
    *,
    seq_ids: Optional[Sequence[str]] = None,
) -> int:
    clock = require_clock_ms(clock_ms)
    round_row = _round_row(db, round_id)
    if round_row["status"] != "open":
        return 0
    policy_version = round_row["policy_version"]
    budget = int(round_row["budget_bytes"])
    planned_count = 0

    with db.immediate() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if round_row["abandon_reason"] == "byte_budget_exhausted":
            return 0
        policies = _policy_for_layers(conn, policy_version)
        layer_rows = {row["layer_id"]: row for row in conn.execute("SELECT * FROM layer_state").fetchall()}

        need_by_layer: Dict[str, int] = {}
        for layer_id, layer in layer_rows.items():
            encumbered = conn.execute(
                """
                SELECT COALESCE(SUM(actual_bytes), 0) AS n
                  FROM eviction_attempts
                 WHERE layer_id=? AND status IN ('planned', 'writing')
                """,
                (layer_id,),
            ).fetchone()["n"]
            projected = int(layer["hbm_used_bytes"]) - int(encumbered)
            target = _target_capacity(int(layer["capacity_bytes"]), policies.get(layer_id))
            need = max(0, projected - target)
            if need > 0:
                need_by_layer[layer_id] = need

        if not need_by_layer:
            return 0

        params: List[Any] = [policy_version]
        seq_filter = ""
        if seq_ids:
            marks = ",".join("?" for _ in seq_ids)
            seq_filter = f"AND e.seq_id IN ({marks})"
            params.extend(seq_ids)
        params.extend(list(need_by_layer.keys()))
        candidates = conn.execute(
            f"""
            SELECT e.*, r.priority,
                   p.cold_order AS policy_cold_order,
                   p.generation AS policy_generation,
                   p.threshold_text AS threshold_text
              FROM kv_entries e
              JOIN requests r ON r.seq_id = e.seq_id
              LEFT JOIN policy_entries p
                ON p.policy_version = ? AND p.layer_id = e.layer_id
             WHERE e.state = 'hbm'
               {seq_filter}
               AND e.layer_id IN ({','.join('?' for _ in need_by_layer)})
               AND NOT EXISTS (
                    SELECT 1 FROM eviction_attempts a
                     WHERE a.entry_id = e.entry_id
                       AND a.status IN ('planned', 'writing', 'committed')
               )
             ORDER BY r.priority ASC, e.seq_id ASC,
                      COALESCE(p.cold_order, 1000) ASC, e.layer_id ASC,
                      e.token_index ASC, e.entry_id ASC
            """,
            params,
        ).fetchall()

        budget_used = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(actual_bytes), 0) AS n
                  FROM eviction_attempts
                 WHERE round_id=? AND status IN ('planned', 'writing', 'committed')
                """,
                (round_id,),
            ).fetchone()["n"]
        )
        max_rank = conn.execute(
            "SELECT COALESCE(MAX(rank_in_round), 0) AS n FROM eviction_attempts WHERE round_id=?",
            (round_id,),
        ).fetchone()["n"]

        for candidate in candidates:
            layer_id = candidate["layer_id"]
            if need_by_layer.get(layer_id, 0) <= 0:
                continue
            size = int(candidate["size_bytes"])
            rank = int(max_rank) + 1
            uid = f"{round_id}:{rank:05d}"
            header = {
                "uid": uid,
                "entry_id": candidate["entry_id"],
                "seq_id": candidate["seq_id"],
                "layer_id": layer_id,
                "token_index": int(candidate["token_index"]),
                "actual_bytes": size,
                "payload_digest": candidate["digest"],
            }
            if budget_used + size > budget:
                conn.execute(
                    """
                    INSERT INTO eviction_attempts(
                        uid, idempotency_key, round_id, rank_in_round, seq_id,
                        layer_id, entry_id, actual_bytes, status, abandon_reason,
                        policy_version, created_clock
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'abandoned', 'byte_budget_exhausted', ?, ?)
                    """,
                    (
                        uid,
                        f"attempt:{uid}",
                        round_id,
                        rank,
                        candidate["seq_id"],
                        layer_id,
                        candidate["entry_id"],
                        size,
                        policy_version,
                        clock,
                    ),
                )
                conn.execute(
                    "UPDATE rounds SET abandon_reason='byte_budget_exhausted', abandon_detail_json=? WHERE round_id=?",
                    (dumps_json({"at_rank": rank, "candidate_entry_id": candidate["entry_id"]}), round_id),
                )
                max_rank = rank
                break

            conversion = conn.execute(
                "SELECT * FROM ledger_conversions WHERE scope_type='kv_entry' AND scope_key=?",
                (candidate["entry_id"],),
            ).fetchone()
            if conversion is None:
                raise KvError(Code.INVARIANT_VIOLATION, f"missing fixed-point conversion for {candidate['entry_id']}")
            conn.execute(
                """
                INSERT INTO eviction_attempts(
                    uid, idempotency_key, round_id, rank_in_round, seq_id, layer_id,
                    entry_id, actual_bytes, status, fixed_value, carry_in, carry_out,
                    error_bound_bytes, conversion_id, policy_version,
                    policy_generation, threshold_text, frame_length, header_json,
                    payload_digest, created_clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    f"attempt:{uid}",
                    round_id,
                    rank,
                    candidate["seq_id"],
                    layer_id,
                    candidate["entry_id"],
                    size,
                    int(conversion["fixed_delta"]),
                    int(conversion["carry_in"]),
                    int(conversion["carry_out"]),
                    int(conversion["error_bound_bytes"]),
                    int(conversion["conversion_id"]),
                    policy_version,
                    candidate["policy_generation"],
                    candidate["threshold_text"],
                    expected_frame_length(header),
                    dumps_json(header),
                    candidate["digest"],
                    clock,
                ),
            )
            budget_used += size
            need_by_layer[layer_id] -= size
            max_rank = rank
            planned_count += 1

        _clock_watermark(db, clock)
    return planned_count


def _abandon_remaining(
    conn: sqlite3.Connection,
    round_id: str,
    reason: str,
    *,
    current_attempt_id: Optional[int] = None,
    layer_id: Optional[str] = None,
) -> int:
    sql = """
        UPDATE eviction_attempts
           SET status='abandoned', abandon_reason=?
         WHERE round_id=? AND status='planned'
    """
    params: List[Any] = [reason, round_id]
    if current_attempt_id is not None:
        sql += " AND attempt_id >= ?"
        params.append(current_attempt_id)
    if layer_id is not None:
        sql += " AND layer_id=?"
        params.append(layer_id)
    cursor = conn.execute(sql, params)
    if reason in {"byte_budget_exhausted", "concurrency_slots_exhausted"}:
        conn.execute(
            """
            UPDATE rounds
               SET abandon_reason=?,
                   abandon_detail_json=?
             WHERE round_id=? AND abandon_reason IS NULL
            """,
            (reason, dumps_json({"layer_id": layer_id}), round_id),
        )
    return cursor.rowcount


def claim_next_attempt(
    db: Database,
    round_id: str,
    worker_id: str,
    clock_ms: int,
) -> Optional[Dict[str, Any]]:
    clock = require_clock_ms(clock_ms)
    config = db.require_config()
    max_concurrent = int(config["max_concurrent"])
    nvme_path, lock_path = db.paths()

    with db.immediate() as conn:
        round_row = conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
        if round_row is None or round_row["status"] != "open":
            return None
        attempt = conn.execute(
            """
            SELECT * FROM eviction_attempts
             WHERE round_id=? AND status='planned'
             ORDER BY rank_in_round ASC
             LIMIT 1
            """,
            (round_id,),
        ).fetchone()
        if attempt is None:
            return None

        policy = conn.execute(
            "SELECT * FROM policy_entries WHERE policy_version=? AND layer_id=?",
            (attempt["policy_version"], attempt["layer_id"]),
        ).fetchone()
        layer = conn.execute(
            "SELECT * FROM layer_state WHERE layer_id=?",
            (attempt["layer_id"],),
        ).fetchone()
        target = _target_capacity(int(layer["capacity_bytes"]), policy)
        writing_same_layer = int(
            conn.execute(
                "SELECT COALESCE(SUM(actual_bytes),0) n FROM eviction_attempts WHERE layer_id=? AND status='writing' AND attempt_id != ?",
                (attempt["layer_id"], attempt["attempt_id"]),
            ).fetchone()["n"]
        )
        projected_used = int(layer["hbm_used_bytes"]) - writing_same_layer
        if projected_used <= target:
            _abandon_remaining(
                conn,
                round_id,
                "memory_pressure_cleared",
                current_attempt_id=int(attempt["attempt_id"]),
                layer_id=attempt["layer_id"],
            )
            return None

        budget = int(round_row["budget_bytes"])
        encumbered = int(
            conn.execute(
                """
                SELECT COALESCE(SUM(actual_bytes),0) n
                  FROM eviction_attempts
                 WHERE round_id=? AND status IN ('writing','committed')
                """,
                (round_id,),
            ).fetchone()["n"]
        )
        size = int(attempt["actual_bytes"])
        if encumbered + size > budget:
            _abandon_remaining(
                conn,
                round_id,
                "byte_budget_exhausted",
                current_attempt_id=int(attempt["attempt_id"]),
            )
            return None

        active_slots = int(
            conn.execute(
                "SELECT COUNT(*) n FROM eviction_attempts WHERE round_id=? AND status='writing'",
                (round_id,),
            ).fetchone()["n"]
        )
        if active_slots >= max_concurrent:
            _abandon_remaining(
                conn,
                round_id,
                "concurrency_slots_exhausted",
                current_attempt_id=int(attempt["attempt_id"]),
            )
            return None

        conn.execute(
            """
            INSERT INTO nvme_files(path, next_offset) VALUES(?, 0)
            ON CONFLICT(path) DO NOTHING
            """,
            (str(nvme_path),),
        )
        file_row = conn.execute("SELECT next_offset FROM nvme_files WHERE path=?", (str(nvme_path),)).fetchone()
        offset = int(file_row["next_offset"])
        frame_length_value = int(attempt["frame_length"])
        conn.execute(
            "UPDATE nvme_files SET next_offset=? WHERE path=?",
            (offset + frame_length_value, str(nvme_path)),
        )
        conn.execute(
            """
            UPDATE eviction_attempts
               SET status='writing', worker_id=?, started_clock=?,
                   file_path=?, file_offset=?
             WHERE attempt_id=? AND status='planned'
            """,
            (worker_id, clock, str(nvme_path), offset, attempt["attempt_id"]),
        )
        claimed = conn.execute(
            "SELECT * FROM eviction_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()
        _clock_watermark(db, clock)
        return dict(claimed)


def mark_attempt_failed(db: Database, uid: str, message: str, clock_ms: int) -> None:
    clock = require_clock_ms(clock_ms)
    with db.immediate() as conn:
        conn.execute(
            """
            UPDATE eviction_attempts
               SET status='failed_started', error_message=?, finished_clock=?
             WHERE uid=? AND status='writing'
            """,
            (message[:1000], clock, uid),
        )
        _clock_watermark(db, clock)


def finalize_claimed_attempt(
    db: Database,
    claim: Dict[str, Any],
    clock_ms: int,
    *,
    already_locked: bool = False,
) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    uid = claim["uid"]
    entry_id = claim["entry_id"]
    seq_id = claim["seq_id"]
    layer_id = claim["layer_id"]
    size = int(claim["actual_bytes"])
    offset = int(claim["file_offset"])
    frame_length_value = int(claim["frame_length"])
    nvme_path = Path(claim["file_path"])
    _, lock_path = db.paths()

    payload = deterministic_payload(entry_id, size)
    header = json.loads(claim["header_json"])
    frame = encode_frame(header, payload)
    if len(frame) != frame_length_value:
        raise KvError(Code.INVARIANT_VIOLATION, f"frame length mismatch for {uid}")
    write_frame_at(nvme_path, lock_path, offset, frame, already_locked=already_locked)
    verified = read_frame(nvme_path, offset, frame_length_value)
    if not verified.get("valid"):
        raise KvError(Code.EVICTION_WRITE_FAILED, f"post-write verification failed for {uid}: {verified.get('reason')}")

    with db.immediate() as conn:
        attempt = conn.execute("SELECT * FROM eviction_attempts WHERE uid=?", (uid,)).fetchone()
        if attempt is None:
            raise KvError(Code.INVARIANT_VIOLATION, f"missing attempt {uid}")
        if attempt["status"] == "committed":
            return _attempt_public_record(attempt)
        if attempt["status"] != "writing":
            raise KvError(Code.INVARIANT_VIOLATION, f"attempt {uid} is not writable: {attempt['status']}")

        request = conn.execute("SELECT request_generation FROM requests WHERE seq_id=?", (seq_id,)).fetchone()
        generation = int(request["request_generation"])
        ledger_key = f"ledger:{seq_id}:gen{generation}:{entry_id}"
        record = {
            "ledger_key": ledger_key,
            "eviction_uid": uid,
            "round_id": claim["round_id"],
            "seq_id": seq_id,
            "layer_id": layer_id,
            "entry_id": entry_id,
            "actual_bytes": size,
            "fixed_value": int(claim["fixed_value"]),
            "carry_in": int(claim["carry_in"]),
            "carry_out": int(claim["carry_out"]),
            "error_bound_bytes": int(claim["error_bound_bytes"]),
            "error_item_bytes": int(claim["carry_out"]),
            "policy_version": claim["policy_version"],
            "policy_generation": claim["policy_generation"],
            "file_offset": offset,
            "payload_digest": claim["payload_digest"],
        }
        response_hash = canonical_hash(record)
        existing = conn.execute(
            "SELECT * FROM idempotent_ledger WHERE idempotency_key=?",
            (ledger_key,),
        ).fetchone()
        if existing is not None:
            if existing["response_hash"] != response_hash:
                raise KvError(Code.IDEMPOTENCY_CONFLICT, f"same ledger key submitted with different value: {ledger_key}")
        else:
            conn.execute(
                "INSERT INTO idempotent_ledger(idempotency_key, response_hash, attempt_uid, created_clock) VALUES (?, ?, ?, ?)",
                (ledger_key, response_hash, uid, clock),
            )

        entry_cursor = conn.execute(
            "UPDATE kv_entries SET state='evicted' WHERE entry_id=? AND state='hbm'",
            (entry_id,),
        )
        if entry_cursor.rowcount != 1:
            committed = conn.execute(
                "SELECT status FROM eviction_attempts WHERE entry_id=? AND status='committed'",
                (entry_id,),
            ).fetchone()
            if committed is not None:
                return record
            raise KvError(Code.INVARIANT_VIOLATION, f"KV entry {entry_id} was not in HBM state")

        req_cursor = conn.execute(
            """
            UPDATE request_layers
               SET hbm_used_bytes = hbm_used_bytes - ?,
                   evicted_bytes = evicted_bytes + ?
             WHERE seq_id=? AND layer_id=? AND hbm_used_bytes >= ?
            """,
            (size, size, seq_id, layer_id, size),
        )
        if req_cursor.rowcount != 1:
            raise KvError(Code.INVARIANT_VIOLATION, f"request-layer ledger update failed for {entry_id}")
        layer_cursor = conn.execute(
            """
            UPDATE layer_state
               SET hbm_used_bytes = hbm_used_bytes - ?,
                   evicted_bytes = evicted_bytes + ?
             WHERE layer_id=? AND hbm_used_bytes >= ?
            """,
            (size, size, layer_id, size),
        )
        if layer_cursor.rowcount != 1:
            raise KvError(Code.INVARIANT_VIOLATION, f"layer ledger update failed for {entry_id}")

        conn.execute(
            "UPDATE eviction_attempts SET status='committed', finished_clock=? WHERE attempt_id=?",
            (clock, attempt["attempt_id"]),
        )
        _clock_watermark(db, clock)
        committed_row = conn.execute("SELECT * FROM eviction_attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
        return _attempt_public_record(committed_row)


def _attempt_public_record(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    return {
        "eviction_uid": data["uid"],
        "round_id": data["round_id"],
        "rank_in_round": data["rank_in_round"],
        "seq_id": data["seq_id"],
        "layer_id": data["layer_id"],
        "entry_id": data["entry_id"],
        "status": data["status"],
        "actual_bytes": data["actual_bytes"],
        "fixed_value": data["fixed_value"],
        "carry_in": data["carry_in"],
        "carry_out": data["carry_out"],
        "error_bound_bytes": data["error_bound_bytes"],
        "error_item_bytes": data["carry_out"],
        "policy_version": data["policy_version"],
        "policy_generation": data["policy_generation"],
        "threshold_text": data["threshold_text"],
        "file_offset": data["file_offset"],
        "frame_length": data["frame_length"],
        "payload_digest": data["payload_digest"],
        "abandon_reason": data["abandon_reason"],
        "error_message": data["error_message"],
    }


class FaultController:
    def __init__(self, sigkill_after_claims: Optional[int] = None, fail_entry_id: Optional[str] = None) -> None:
        import threading

        self.sigkill_after_claims = sigkill_after_claims
        self.fail_entry_id = fail_entry_id
        self._lock = threading.Lock()
        self._claims = 0

    def after_claim(self, claim: Dict[str, Any]) -> None:
        if self.fail_entry_id and claim["entry_id"] == self.fail_entry_id:
            raise KvError(Code.EVICTION_WRITE_FAILED, "injected started-write failure")
        if self.sigkill_after_claims is not None:
            with self._lock:
                self._claims += 1
                hit = self._claims >= int(self.sigkill_after_claims)
            if hit:
                os.kill(os.getpid(), 9)


def _eviction_worker(
    workspace: Path,
    round_id: str,
    worker_index: int,
    clock_ms: int,
    fault: FaultController,
) -> List[Dict[str, Any]]:
    worker_id = f"pid{os.getpid()}-worker{worker_index}-{uuid.uuid4().hex[:8]}"
    db = Database(workspace)
    records: List[Dict[str, Any]] = []
    try:
        while True:
            claim = claim_next_attempt(db, round_id, worker_id, clock_ms)
            if claim is None:
                break
            try:
                fault.after_claim(claim)
                record = finalize_claimed_attempt(db, claim, clock_ms)
                records.append(record)
            except BaseException as exc:  # injected failures must become failed_started
                try:
                    message = exc.message if isinstance(exc, KvError) else str(exc)
                    mark_attempt_failed(db, claim["uid"], f"{type(exc).__name__}: {message}", clock_ms)
                except Exception:
                    pass
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
    finally:
        db.close()
    return records


def round_summary(db: Database, round_id: str) -> Dict[str, Any]:
    round_row = db.conn.execute("SELECT * FROM rounds WHERE round_id=?", (round_id,)).fetchone()
    if round_row is None:
        raise KvError(Code.STATE_NOT_INITIALIZED, f"unknown round {round_id}")
    attempts = db.conn.execute(
        "SELECT * FROM eviction_attempts WHERE round_id=? ORDER BY rank_in_round ASC, attempt_id ASC",
        (round_id,),
    ).fetchall()
    counts: Dict[str, int] = {}
    bytes_by_status: Dict[str, int] = {}
    records: List[Dict[str, Any]] = []
    abandoned: List[Dict[str, Any]] = []
    for row in attempts:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        bytes_by_status[row["status"]] = bytes_by_status.get(row["status"], 0) + int(row["actual_bytes"])
        public = _attempt_public_record(row)
        if row["status"] == "committed":
            records.append(public)
        elif row["status"] == "abandoned":
            abandoned.append(
                {
                    "rank_in_round": row["rank_in_round"],
                    "seq_id": row["seq_id"],
                    "layer_id": row["layer_id"],
                    "entry_id": row["entry_id"],
                    "actual_bytes": row["actual_bytes"],
                    "reason": row["abandon_reason"],
                }
            )
    return {
        "round_id": round_id,
        "status": round_row["status"],
        "policy_version": round_row["policy_version"],
        "budget_bytes": round_row["budget_bytes"],
        "max_concurrent": round_row["max_concurrent"],
        "abandon_reason": round_row["abandon_reason"],
        "counts": dict(sorted(counts.items())),
        "bytes_by_status": dict(sorted(bytes_by_status.items())),
        "committed_bytes": bytes_by_status.get("committed", 0),
        "evictions": records,
        "abandoned": abandoned,
    }


def run_eviction_round(
    db: Database,
    round_id: str,
    clock_ms: int,
    *,
    seq_ids: Optional[Sequence[str]] = None,
    shard_count: Optional[int] = None,
    shard_index: Optional[int] = None,
    sigkill_after_claims: Optional[int] = None,
    fail_entry_id: Optional[str] = None,
) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    create_result = create_round(db, round_id, clock)
    existing_round = _round_row(db, round_id)
    if not create_result["created"]:
        if existing_round["status"] in {"completed", "completed_with_failures"}:
            summary = round_summary(db, round_id)
            summary["noop"] = True
            return summary
        unfinished = db.conn.execute(
            "SELECT COUNT(*) n FROM eviction_attempts WHERE round_id=? AND status IN ('planned','writing')",
            (round_id,),
        ).fetchone()["n"]
        if unfinished:
            raise KvError(
                Code.RECOVERY_REQUIRED,
                f"round {round_id} has {unfinished} unfinished attempts; run resume before evict",
            )

    rows = db.conn.execute("SELECT seq_id FROM requests ORDER BY priority ASC, seq_id ASC").fetchall()
    all_seq = [row["seq_id"] for row in rows]
    selected = list(seq_ids) if seq_ids is not None else all_seq
    if shard_count is not None:
        if shard_count <= 0 or shard_index is None or not (0 <= shard_index < shard_count):
            raise KvError(Code.CLI_USAGE, "invalid shard count/index")
        selected = [seq for index, seq in enumerate(all_seq) if index % shard_count == shard_index]
    unknown = sorted(set(selected) - set(all_seq))
    if unknown:
        raise KvError(Code.STATE_NOT_INITIALIZED, f"unknown seq_id values: {unknown}")

    for seq_id in selected:
        while True:
            token = advance_next_token(db, seq_id, clock)
            if token is None:
                break
            plan_pressure_candidates(db, round_id, clock, seq_ids=selected)
        plan_pressure_candidates(db, round_id, clock, seq_ids=selected)

    planned_count = db.conn.execute(
        "SELECT COUNT(*) n FROM eviction_attempts WHERE round_id=? AND status='planned'",
        (round_id,),
    ).fetchone()["n"]
    config = db.require_config()
    worker_count = max(1, min(int(config["max_concurrent"]), max(1, planned_count)))
    fault = FaultController(sigkill_after_claims=sigkill_after_claims, fail_entry_id=fail_entry_id)

    if planned_count:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"evict-{round_id}") as pool:
            futures = [
                pool.submit(_eviction_worker, db.workspace, round_id, index, clock, fault)
                for index in range(worker_count)
            ]
            for future in as_completed(futures):
                future.result()

    with db.immediate() as conn:
        failed = conn.execute(
            "SELECT COUNT(*) n FROM eviction_attempts WHERE round_id=? AND status='failed_started'",
            (round_id,),
        ).fetchone()["n"]
        unfinished = conn.execute(
            "SELECT COUNT(*) n FROM eviction_attempts WHERE round_id=? AND status IN ('planned','writing')",
            (round_id,),
        ).fetchone()["n"]
        if unfinished:
            status = "interrupted"
        elif failed:
            status = "completed_with_failures"
        else:
            status = "completed"
        conn.execute(
            "UPDATE rounds SET status=?, closed_clock=? WHERE round_id=?",
            (status, clock, round_id),
        )
        _clock_watermark(db, clock)

    summary = round_summary(db, round_id)
    summary["selected_seq_ids"] = selected
    summary["worker_count"] = worker_count
    return summary


def _clock_watermark(db: Database, clock_ms: int) -> int:
    current = db.get_meta("logical_clock_watermark_ms")
    value = int(current) if current is not None else 0
    value = max(value, clock_ms)
    db.set_meta("logical_clock_watermark_ms", str(value))
    return value


def load_inputs(
    db: Database,
    requests_path: str | Path,
    config_path: str | Path,
    clock_ms: int,
) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    raw_requests = read_json(requests_path)
    request_specs = validate_requests_payload(raw_requests)
    layer_ids = {layer.layer_id for request in request_specs for layer in request.layers}
    config = validate_memory_config_file(config_path, layer_ids)
    input_hash = canonical_hash({"requests": raw_requests, "config": read_json(config_path, parse_decimal=True)})

    existing = db.conn.execute("SELECT * FROM config WHERE id=1").fetchone()
    if existing is not None:
        prior_hash = db.get_meta("input_hash")
        if prior_hash == input_hash:
            return {
                "command": "load",
                "ok": True,
                "noop": True,
                "seq_count": db.conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"],
                "input_hash": input_hash,
            }
        raise KvError(
            Code.STATE_ALREADY_LOADED,
            "workspace already contains a different load; use a new --workspace for another sample",
        )

    request_by_id = {item["seq_id"]: item for item in raw_requests["requests"]}
    with db.immediate() as conn:
        _clock_watermark(db, clock)
        conn.execute(
            """
            INSERT INTO config(
                id, payload_json, total_memory_bytes, budget_bytes, max_concurrent,
                scale, workspace, nvme_path, lock_path, loaded_clock
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dumps_json(read_json(config_path, parse_decimal=True)),
                config.total_memory_bytes,
                config.eviction_budget_bytes,
                config.max_concurrent_evictions,
                config.fixed_point.scale,
                str(db.workspace),
                config.nvme_file,
                config.lock_file,
                clock,
            ),
        )
        db.set_meta("input_hash", input_hash)

        for layer_id in sorted(config.layer_capacity_bytes):
            capacity = config.layer_capacity_bytes[layer_id]
            ratio = config.ratios[layer_id]
            exact_capacity = __import__("decimal").Decimal(config.total_memory_bytes) * ratio
            fractional = exact_capacity - capacity
            conversion = add_conversion(
                conn,
                scope_type="layer_capacity",
                scope_key=f"layer:{layer_id}",
                source_value_text=str(exact_capacity),
                scale=config.fixed_point.scale,
                clock_ms=clock,
                value=capacity,
                detail={"ratio": str(ratio), "fractional_byte_remainder": str(fractional)},
            )
            conn.execute(
                "INSERT INTO layer_state(layer_id, ratio_text, capacity_bytes, fractional_capacity_text) VALUES (?, ?, ?, ?)",
                (layer_id, str(ratio), capacity, str(fractional)),
            )

        add_conversion(
            conn,
            scope_type="eviction_budget",
            scope_key="round_budget",
            source_value_text=str(config.eviction_budget_bytes),
            scale=config.fixed_point.scale,
            clock_ms=clock,
            value=config.eviction_budget_bytes,
        )
        add_conversion(
            conn,
            scope_type="total_memory",
            scope_key="memory",
            source_value_text=str(config.total_memory_bytes),
            scale=config.fixed_point.scale,
            clock_ms=clock,
            value=config.total_memory_bytes,
        )

        active_policy = db.get_meta("active_policy_version")
        for request in request_specs:
            raw = request_by_id[request.seq_id]
            conn.execute(
                """
                INSERT INTO requests(
                    seq_id, priority, max_len, generated_tokens, request_generation,
                    watermark_token, status, bound_policy_version, payload_json,
                    created_clock, updated_clock
                ) VALUES (?, ?, ?, ?, 1, 0, 'loaded', ?, ?, ?, ?)
                """,
                (
                    request.seq_id,
                    request.priority,
                    request.max_len,
                    request.generated_tokens,
                    active_policy,
                    dumps_json(raw),
                    clock,
                    clock,
                ),
            )
            for layer in request.layers:
                conn.execute(
                    """
                    INSERT INTO request_layers(seq_id, layer_id)
                    VALUES (?, ?)
                    """,
                    (request.seq_id, layer.layer_id),
                )

    return {
        "command": "load",
        "ok": True,
        "noop": False,
        "workspace": str(db.workspace),
        "db_path": str(db.db_path),
        "seq_count": len(request_specs),
        "layer_count": len(config.layer_capacity_bytes),
        "input_hash": input_hash,
        "total_memory_bytes": config.total_memory_bytes,
        "layer_capacity_bytes": dict(sorted(config.layer_capacity_bytes.items())),
        "fixed_point_scale": config.fixed_point.scale,
    }


def apply_merge(
    db: Database,
    package_path: str | Path,
    clock_ms: int,
    *,
    sigkill_after_stage: bool = False,
) -> Dict[str, Any]:
    clock = require_clock_ms(clock_ms)
    loaded = load_and_merge_package(package_path, clock)
    version = f"pv{loaded.manifest_version:04d}-{loaded.content_hash[:12]}"

    with db.immediate() as conn:
        _clock_watermark(db, clock)
        current_version = db.get_meta("active_policy_version")
        current_row = None
        if current_version:
            current_row = conn.execute(
                "SELECT * FROM policy_versions WHERE version=?",
                (current_version,),
            ).fetchone()

        if current_row is not None:
            current_manifest = int(current_row["manifest_version"])
            if loaded.manifest_version < current_manifest:
                raise KvError(
                    Code.POLICY_VERSION_ROLLBACK,
                    "refusing to roll back policy manifest version",
                    details={"active": current_manifest, "incoming": loaded.manifest_version},
                )
            if loaded.manifest_version == current_manifest and current_row["content_hash"] != loaded.content_hash:
                raise KvError(Code.POLICY_VERSION_CONFLICT, "same manifest version has different content hash")
            current_floor = int(current_row["generation_floor"])
            if loaded.generation_floor < current_floor:
                raise KvError(
                    Code.POLICY_GENERATION_FLOOR,
                    "incoming policy generation floor is lower than applied floor",
                    details={"active_floor": current_floor, "incoming_floor": loaded.generation_floor},
                )

        applied = conn.execute(
            "SELECT * FROM policy_applied WHERE manifest_version=?",
            (loaded.manifest_version,),
        ).fetchone()
        if applied is not None:
            if applied["content_hash"] == loaded.content_hash:
                db.set_meta("active_policy_version", applied["version"])
                return {
                    "command": "merge",
                    "ok": True,
                    "decision": "noop_previously_applied",
                    "active_policy_version": applied["version"],
                    "manifest_version": loaded.manifest_version,
                }
            raise KvError(
                Code.POLICY_VERSION_CONFLICT,
                f"manifest version {loaded.manifest_version} was already applied with different content",
            )

        if current_row is None:
            decision = "full_replace_initial"
        elif loaded.snapshot_type == "incremental":
            decision = "incremental_append"
        else:
            decision = "full_replace"

        conn.execute(
            """
            INSERT INTO policy_versions(
                version, manifest_version, content_hash, snapshot_type,
                generation_floor, declared_clock_ms, applied_clock_ms,
                package_path, report_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                loaded.manifest_version,
                loaded.content_hash,
                loaded.snapshot_type,
                loaded.generation_floor,
                loaded.declared_clock_ms,
                clock,
                str(Path(package_path).resolve()),
                dumps_json(
                    {
                        "decisions": loaded.decisions,
                        "time_conversions": loaded.time_conversions,
                        "tombstones": loaded.tombstones,
                        "decision": decision,
                        "sources": loaded.sources,
                    }
                ),
            ),
        )

        stage_exists = conn.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='policy_stage'"
        ).fetchone()
        if stage_exists is None:
            conn.execute("CREATE TEMP TABLE policy_stage AS SELECT * FROM policy_entries WHERE 0")
        else:
            conn.execute("DELETE FROM policy_stage")
        staged_rows: List[tuple[Any, ...]] = []
        for entry in loaded.entries:
            staged_rows.append(
                (
                    version,
                    entry["policy_key"],
                    entry["layer_id"],
                    entry["source_format"],
                    str(entry["source_version"]),
                    int(entry["generation"]),
                    int(entry["declared_clock_ms"]),
                    dumps_json(entry["original_time_repr"]),
                    entry["threshold_text"],
                    int(entry["cold_order"]),
                    dumps_json(entry.get("rejected_sources", [])),
                )
            )
        conn.executemany(
            """
            INSERT INTO policy_stage(
                policy_version, policy_key, layer_id, source_format, source_version,
                generation, declared_clock_ms, original_time_json, threshold_text,
                cold_order, rejected_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            staged_rows,
        )

        if sigkill_after_stage:
            os.kill(os.getpid(), 9)

        if decision == "incremental_append" and current_version:
            conn.execute(
                """
                INSERT INTO policy_entries
                SELECT ? AS policy_version, policy_key, layer_id, source_format,
                       source_version, generation, declared_clock_ms, original_time_json,
                       threshold_text, cold_order, rejected_json
                FROM policy_entries WHERE policy_version=?
                  AND policy_key NOT IN (SELECT policy_key FROM policy_stage)
                """,
                (version, current_version),
            )
            for tombstone in loaded.tombstones:
                conn.execute(
                    "DELETE FROM policy_entries WHERE policy_version=? AND policy_key=?",
                    (version, tombstone["policy_key"]),
                )
        conn.execute(
            """
            INSERT INTO policy_entries(
                policy_version, policy_key, layer_id, source_format, source_version,
                generation, declared_clock_ms, original_time_json, threshold_text,
                cold_order, rejected_json
            )
            SELECT policy_version, policy_key, layer_id, source_format, source_version,
                   generation, declared_clock_ms, original_time_json, threshold_text,
                   cold_order, rejected_json
            FROM policy_stage
            """
        )
        conn.execute(
            "INSERT INTO policy_applied(manifest_version, version, content_hash, applied_clock_ms) VALUES (?, ?, ?, ?)",
            (loaded.manifest_version, version, loaded.content_hash, clock),
        )
        db.set_meta("active_policy_version", version)
        _clock_watermark(db, clock)
        unified_policy_count = conn.execute(
            "SELECT COUNT(*) n FROM policy_entries WHERE policy_version=?",
            (version,),
        ).fetchone()["n"]

    return {
        "command": "merge",
        "ok": True,
        "decision": decision,
        "active_policy_version": version,
        "manifest_version": loaded.manifest_version,
        "content_hash": loaded.content_hash,
        "sources": loaded.sources,
        "policy_count": unified_policy_count,
        "incoming_policy_count": len(loaded.entries),
        "tombstone_count": len(loaded.tombstones),
        "decisions": loaded.decisions,
        "time_conversions": loaded.time_conversions,
        "tombstones": loaded.tombstones,
    }
