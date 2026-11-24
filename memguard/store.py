"""SQLite-backed boundary ledger.

Tables:
  meta         - layout/checkpoint versions, batch watermark
  decls        - every accepted declaration (history kept for arbitration)
  regions      - resolved live regions (replaced wholesale on change)
  traces       - normalized accepted trace records (source of truth)
  ledger       - (region_id, access_kind) counters + seq watermark
  evidence     - per-violation evidence chain rows
  rejects      - rejection log (side channel, not part of ledger digest)
  checkpoints  - per-batch ledger digests

Every batch import runs in ONE transaction. Ledger rows for touched or
affected regions are recomputed deterministically from the traces table,
which makes batched/out-of-order arrival equivalent to a from-scratch
serial pass by construction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from . import CHECKPOINT_STRUCT_VERSION, LEDGER_LAYOUT_VERSION, codes
from .judge import judge_access
from .model import RegionDecl, TraceRecord
from .resolution import affected_regions, resolve

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decls (
    region_id TEXT NOT NULL, generation INTEGER NOT NULL, tombstone INTEGER NOT NULL,
    arena_size INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    life_start_ms INTEGER NOT NULL, life_end_ms INTEGER,
    parent_id TEXT, file_id TEXT NOT NULL, line_no INTEGER NOT NULL,
    PRIMARY KEY (region_id, generation, tombstone, file_id, line_no));
CREATE TABLE IF NOT EXISTS regions (
    region_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
    arena_size INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    life_start_ms INTEGER NOT NULL, life_end_ms INTEGER, parent_id TEXT);
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY, access_seq INTEGER NOT NULL, region_id TEXT NOT NULL,
    base_offset INTEGER NOT NULL, access_size INTEGER NOT NULL,
    access_kind TEXT NOT NULL, arena_size INTEGER NOT NULL, ts_ms INTEGER NOT NULL,
    file_id TEXT NOT NULL, line_no INTEGER NOT NULL, batch_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    region_id TEXT NOT NULL, access_kind TEXT NOT NULL,
    total_count INTEGER NOT NULL, oob_count INTEGER NOT NULL,
    oob_max_end INTEGER NOT NULL, watermark INTEGER NOT NULL, dup_count INTEGER NOT NULL,
    PRIMARY KEY (region_id, access_kind));
CREATE TABLE IF NOT EXISTS evidence (
    region_id TEXT NOT NULL, access_kind TEXT NOT NULL, trace_id TEXT NOT NULL,
    access_seq INTEGER NOT NULL, code TEXT NOT NULL,
    base_offset INTEGER NOT NULL, access_size INTEGER NOT NULL,
    PRIMARY KEY (region_id, access_kind, trace_id));
CREATE TABLE IF NOT EXISTS rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL, line_no INTEGER NOT NULL, code TEXT NOT NULL,
    detail TEXT NOT NULL, batch_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints (
    batch_seq INTEGER PRIMARY KEY, digest TEXT NOT NULL, created_ms INTEGER NOT NULL);
"""

DIGEST_TABLES = ("regions", "traces", "ledger", "evidence")


class LayoutMismatch(Exception):
    pass


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _check_layout(conn)
    return conn


def _check_layout(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='ledger_layout_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('ledger_layout_version', ?)",
                     (str(LEDGER_LAYOUT_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('checkpoint_struct_version', ?)",
                     (str(CHECKPOINT_STRUCT_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('batch_watermark', '0')")
        conn.commit()
        return
    if int(row[0]) != LEDGER_LAYOUT_VERSION:
        # Layout version changed: full rebuild instead of incremental join.
        conn.executescript(
            "DROP TABLE IF EXISTS decls; DROP TABLE IF EXISTS regions;"
            "DROP TABLE IF EXISTS traces; DROP TABLE IF EXISTS ledger;"
            "DROP TABLE IF EXISTS evidence; DROP TABLE IF EXISTS rejects;"
            "DROP TABLE IF EXISTS checkpoints; DELETE FROM meta;")
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta(key, value) VALUES ('ledger_layout_version', ?)",
                     (str(LEDGER_LAYOUT_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('checkpoint_struct_version', ?)",
                     (str(CHECKPOINT_STRUCT_VERSION),))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('batch_watermark', '0')")
        conn.commit()
        raise LayoutMismatch(codes.E_VERSION)


def batch_watermark(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='batch_watermark'").fetchone()
    return int(row[0]) if row else 0


def load_regions(conn):
    regions = {}
    for row in conn.execute(
            "SELECT region_id, generation, arena_size, start, end,"
            " life_start_ms, life_end_ms, parent_id FROM regions"):
        regions[row[0]] = RegionDecl(
            region_id=row[0], generation=row[1], tombstone=False, arena_size=row[2],
            start=row[3], end=row[4], life_start_ms=row[5], life_end_ms=row[6],
            parent_id=row[7])
    return regions


def load_decl_history(conn):
    decls = []
    for row in conn.execute(
            "SELECT region_id, generation, tombstone, arena_size, start, end,"
            " life_start_ms, life_end_ms, parent_id, file_id, line_no FROM decls"):
        decls.append(RegionDecl(
            region_id=row[0], generation=row[1], tombstone=bool(row[2]),
            arena_size=row[3], start=row[4], end=row[5], life_start_ms=row[6],
            life_end_ms=row[7], parent_id=row[8], file_id=row[9], line_no=row[10]))
    return decls


def load_traces(conn, region_ids=None):
    sql = ("SELECT trace_id, access_seq, region_id, base_offset, access_size,"
           " access_kind, arena_size, ts_ms, file_id, line_no FROM traces")
    params = ()
    if region_ids is not None:
        if not region_ids:
            return []
        marks = ",".join("?" for _ in region_ids)
        sql += f" WHERE region_id IN ({marks})"
        params = tuple(sorted(region_ids))
    out = []
    for row in conn.execute(sql, params):
        out.append(TraceRecord(
            trace_id=row[0], access_seq=row[1], region_id=row[2],
            base_offset=row[3], access_size=row[4], access_kind=row[5],
            arena_size=row[6], ts_ms=row[7], file_id=row[8], line_no=row[9]))
    return out


def ledger_digest(conn):
    """Stable SHA-256 over the canonical dump of the ledger tables."""
    h = hashlib.sha256()
    for table in DIGEST_TABLES:
        h.update(table.encode())
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3")
        for row in cur:
            h.update(json.dumps(row, default=str, sort_keys=True).encode())
            h.update(b"\n")
    return h.hexdigest()


def recompute_regions(conn, region_ids, regions):
    """Deterministically rebuild ledger+evidence rows for region_ids.

    Canonical order: (access_seq, trace_id). The first record seen for a
    given access_seq is the truth; later ones only bump dup_count.
    """
    region_ids = set(region_ids)
    for rid in region_ids:
        conn.execute("DELETE FROM ledger WHERE region_id=?", (rid,))
        conn.execute("DELETE FROM evidence WHERE region_id=?", (rid,))
    if not region_ids:
        return
    traces = [t for t in load_traces(conn, region_ids) if t.region_id in regions]
    by_key = {}
    for rec in traces:
        by_key.setdefault((rec.region_id, rec.access_kind), []).append(rec)
    for (rid, kind), recs in by_key.items():
        recs.sort(key=lambda r: (r.access_seq, r.trace_id))
        total = oob = dup = 0
        oob_max_end = 0
        watermark = -1
        seen_seq = set()
        for rec in recs:
            watermark = max(watermark, rec.access_seq)
            if rec.access_seq in seen_seq:
                dup += 1
                continue
            seen_seq.add(rec.access_seq)
            total += 1
            verdict = judge_access(rec, regions)
            if verdict is not None:
                oob += 1
                oob_max_end = max(oob_max_end, rec.base_offset + rec.access_size)
                conn.execute(
                    "INSERT INTO evidence(region_id, access_kind, trace_id, access_seq,"
                    " code, base_offset, access_size) VALUES (?,?,?,?,?,?,?)",
                    (rid, kind, rec.trace_id, rec.access_seq, verdict,
                     rec.base_offset, rec.access_size))
        conn.execute(
            "INSERT INTO ledger(region_id, access_kind, total_count, oob_count,"
            " oob_max_end, watermark, dup_count) VALUES (?,?,?,?,?,?,?)",
            (rid, kind, total, oob, oob_max_end, watermark, dup))


def gaps_for(conn, region_id, kind):
    """Missing access_seq intervals inside [min_seen, watermark]."""
    rows = conn.execute(
        "SELECT DISTINCT access_seq FROM traces WHERE region_id=? AND access_kind=?"
        " ORDER BY access_seq", (region_id, kind)).fetchall()
    seqs = [r[0] for r in rows]
    if len(seqs) < 2:
        return []
    gaps = []
    for prev, nxt in zip(seqs, seqs[1:]):
        if nxt > prev + 1:
            gaps.append([prev + 1, nxt - 1])
    return gaps


def apply_batch(conn, new_decls, new_traces, parse_rejects, now_ms):
    """Advance the ledger by one batch inside a single transaction.

    Pre-validation (declaration resolution) happens before any ledger
    mutation; on failure nothing is applied and the caller persists the
    rejects via record_rejects_only. Returns (batch_seq, rejects, affected).
    """
    old_regions = load_regions(conn)
    history = load_decl_history(conn) + list(new_decls)
    new_regions, resolution_rejects = resolve(history)
    if resolution_rejects:
        return None, resolution_rejects, set()

    affected = affected_regions(old_regions, new_regions)
    touched = {t.region_id for t in new_traces}
    recompute = (affected | touched) & set(new_regions)
    dropped = affected - set(new_regions)  # tombstoned / removed regions

    batch_seq = batch_watermark(conn) + 1
    with conn:  # single transaction; any exception rolls everything back
        for decl in new_decls:
            conn.execute(
                "INSERT OR IGNORE INTO decls(region_id, generation, tombstone,"
                " arena_size, start, end, life_start_ms, life_end_ms, parent_id,"
                " file_id, line_no) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (decl.region_id, decl.generation, int(decl.tombstone), decl.arena_size,
                 decl.start, decl.end, decl.life_start_ms, decl.life_end_ms,
                 decl.parent_id, decl.file_id, decl.line_no))
        if affected or not old_regions:
            conn.execute("DELETE FROM regions")
            for rid in sorted(new_regions):
                d = new_regions[rid]
                conn.execute(
                    "INSERT INTO regions(region_id, generation, arena_size, start, end,"
                    " life_start_ms, life_end_ms, parent_id) VALUES (?,?,?,?,?,?,?,?)",
                    (rid, d.generation, d.arena_size, d.start, d.end,
                     d.life_start_ms, d.life_end_ms, d.parent_id))
        dup_trace_rejects = []
        for rec in new_traces:
            try:
                conn.execute(
                    "INSERT INTO traces(trace_id, access_seq, region_id, base_offset,"
                    " access_size, access_kind, arena_size, ts_ms, file_id, line_no,"
                    " batch_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rec.trace_id, rec.access_seq, rec.region_id, rec.base_offset,
                     rec.access_size, rec.access_kind, rec.arena_size, rec.ts_ms,
                     rec.file_id, rec.line_no, batch_seq))
            except sqlite3.IntegrityError:
                dup_trace_rejects.append(rec)
        for rid in dropped:
            conn.execute("DELETE FROM ledger WHERE region_id=?", (rid,))
            conn.execute("DELETE FROM evidence WHERE region_id=?", (rid,))
        recompute_regions(conn, recompute, new_regions)
        conn.execute("UPDATE meta SET value=? WHERE key='batch_watermark'", (str(batch_seq),))
        digest = ledger_digest(conn)
        conn.execute("INSERT INTO checkpoints(batch_seq, digest, created_ms) VALUES (?,?,?)",
                     (batch_seq, digest, now_ms))
        for rej in parse_rejects:
            conn.execute(
                "INSERT INTO rejects(file_id, line_no, code, detail, batch_seq)"
                " VALUES (?,?,?,?,?)",
                (rej.file_id, rej.line_no, rej.code, rej.detail, batch_seq))
        for rec in dup_trace_rejects:
            conn.execute(
                "INSERT INTO rejects(file_id, line_no, code, detail, batch_seq)"
                " VALUES (?,?,?,?,?)",
                (rec.file_id, rec.line_no, codes.E_DUP_TRACE,
                 f"trace_id {rec.trace_id!r} already imported", batch_seq))
    return batch_seq, [], affected


def record_rejects_only(conn, rejects):
    """Persist rejects without touching ledger state (pre-validation failure)."""
    batch_seq = batch_watermark(conn)
    with conn:
        for rej in rejects:
            conn.execute(
                "INSERT INTO rejects(file_id, line_no, code, detail, batch_seq)"
                " VALUES (?,?,?,?,?)",
                (rej.file_id, rej.line_no, rej.code, rej.detail, batch_seq))
