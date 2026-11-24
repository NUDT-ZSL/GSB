"""Batch orchestration: import, recovery boundary, verification."""

from __future__ import annotations

import os
import time

from . import codes, store
from .checkpoint import (FileLock, LockConflict, SnapshotVersionMismatch,
                         read_snapshot, write_snapshot)
from .parser import parse_declarations, parse_traces

DB_NAME = "guard.db"


def db_path(workdir):
    return os.path.join(workdir, DB_NAME)


def _now_ms():
    return int(time.time() * 1000)


def _ledger_summary(conn):
    rows = conn.execute(
        "SELECT region_id, access_kind, total_count, oob_count, watermark"
        " FROM ledger ORDER BY region_id, access_kind").fetchall()
    return [
        {"region_id": r[0], "access_kind": r[1], "total_count": r[2],
         "oob_count": r[3], "watermark": r[4]}
        for r in rows
    ]


def recover(workdir, conn):
    """Enforce the recovery boundary against the last complete checkpoint.

    Returns a list of anomaly dicts (empty = clean). On digest mismatch
    (half-written residue) the ledger is rebuilt from the traces table,
    which is the source of truth, and a fresh checkpoint digest is stored.
    """
    anomalies = []
    try:
        snap = read_snapshot(workdir)
    except SnapshotVersionMismatch as exc:
        anomalies.append({"code": codes.E_VERSION, "detail": str(exc)})
        snap = None
    row = conn.execute(
        "SELECT batch_seq, digest FROM checkpoints ORDER BY batch_seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return anomalies
    batch_seq, expected = row
    actual = store.ledger_digest(conn)
    if actual != expected:
        anomalies.append({
            "code": codes.E_CHECKPOINT,
            "detail": f"ledger digest mismatch at batch {batch_seq}: "
                      f"expected {expected[:12]} got {actual[:12]}; rebuilt from traces",
        })
        regions = store.load_regions(conn)
        store.recompute_regions(conn, list(regions), regions)
        with conn:
            digest = store.ledger_digest(conn)
            conn.execute("UPDATE checkpoints SET digest=?, created_ms=? WHERE batch_seq=?",
                         (digest, _now_ms(), batch_seq))
    # The DB checkpoint is authoritative once its digest verifies. A stale
    # or missing snapshot (e.g. killed before replacement) is refreshed.
    db_digest = conn.execute(
        "SELECT digest FROM checkpoints WHERE batch_seq=?", (batch_seq,)).fetchone()[0]
    if snap is None or snap.get("batch_seq") != batch_seq \
            or snap.get("ledger_digest") != db_digest:
        write_snapshot(workdir, batch_seq, db_digest, _ledger_summary(conn))
    return anomalies


def import_batch(workdir, decl_files, trace_files):
    """Import one batch (declarations + trace files) under the writer lock.

    A file that fails to parse never aborts its siblings. Returns a result
    dict with per-file rejects and the batch watermark.
    """
    os.makedirs(workdir, exist_ok=True)
    outcome = {"workdir": workdir, "batch_seq": None, "rejects": [],
               "anomalies": [], "imported_traces": 0, "imported_decls": 0,
               "code": None}
    try:
        lock = FileLock(workdir).acquire()
    except LockConflict as exc:
        outcome["code"] = codes.E_LOCK
        outcome["rejects"].append({"file_id": "", "line_no": 0,
                                   "code": codes.E_LOCK, "detail": str(exc)})
        return outcome
    try:
        conn = store.connect(db_path(workdir))
        try:
            outcome["anomalies"] = recover(workdir, conn)
            decls, traces, rejects = [], [], []
            for path in decl_files:
                parsed = parse_declarations(path)
                decls.extend(parsed.decls)
                rejects.extend(parsed.rejects)
            for path in trace_files:
                parsed = parse_traces(path)
                traces.extend(parsed.traces)
                rejects.extend(parsed.rejects)
            batch_seq, resolution_rejects, affected = store.apply_batch(
                conn, decls, traces, rejects, _now_ms())
            if batch_seq is None:
                # Pre-validation failed: ledger untouched, persist rejects only.
                store.record_rejects_only(conn, resolution_rejects + rejects)
                outcome["rejects"] = [r.as_dict() for r in resolution_rejects + rejects]
                outcome["code"] = resolution_rejects[0].code
                return outcome
            outcome["batch_seq"] = batch_seq
            outcome["imported_traces"] = len(traces)
            outcome["imported_decls"] = len(decls)
            outcome["rejects"] = [r.as_dict() for r in rejects]
            digest = store.ledger_digest(conn)
            if os.environ.get("MEMGUARD_CRASH_AFTER_COMMIT"):
                # Test hook: simulate a forced kill after the DB commit but
                # before the snapshot is replaced. No exit report promised.
                conn.close()
                os._exit(86)
            write_snapshot(workdir, batch_seq, digest, _ledger_summary(conn))
            return outcome
        finally:
            conn.close()
    finally:
        lock.release()


def verify_checkpoint(workdir):
    """Check DB checkpoint chain and snapshot consistency."""
    result = {"workdir": workdir, "ok": True, "anomalies": [], "code": None}
    if not os.path.exists(db_path(workdir)):
        result["ok"] = False
        result["code"] = codes.E_CHECKPOINT
        result["anomalies"].append({"code": codes.E_CHECKPOINT,
                                    "detail": "no ledger database found"})
        return result
    conn = store.connect(db_path(workdir))
    try:
        anomalies = recover(workdir, conn)
        result["anomalies"] = anomalies
        result["ok"] = not anomalies
        if anomalies:
            result["code"] = anomalies[0]["code"]
        row = conn.execute(
            "SELECT batch_seq, digest FROM checkpoints ORDER BY batch_seq DESC LIMIT 1"
        ).fetchone()
        if row:
            result["batch_seq"] = row[0]
            result["ledger_digest"] = store.ledger_digest(conn)
            result["digest_matches"] = result["ledger_digest"] == row[1]
        return result
    finally:
        conn.close()
