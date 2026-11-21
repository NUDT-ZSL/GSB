"""Operation orchestration: load / replace / resolve / warmup / resume /
diagnose / status / verify. Every mutating operation is journaled so a
force-killed process can be resumed with byte-identical ledger output.
"""
import hashlib
import json
import os

from .errors import (
    BizError, E_SNAPSHOT_VERSION_ROLLBACK, E_REPLACE_CONFLICT, E_NOT_FOUND,
)
from .merge import merge_snapshots, fold_min_expiry
from .precision import analyze_comparisons, analyze_expiry
from .resolver import ResolveSession, conversion_of
from .snapshot_v1 import load_v1
from .snapshot_v2 import load_v2


def _digest(obj):
    raw = json.dumps(obj, sort_keys=True).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _op_id(kind, payload):
    return f"{kind}:{_digest(payload)}"


def _file_digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()[:16]


def _get_meta_value(store, key, default=None):
    conn = store.connect()
    try:
        return store.get_meta(conn, key, default)
    finally:
        conn.close()


def _generation_high_water(store):
    return int(_get_meta_value(store, "generation_high_water", 0))


def _fold_and_findings(entries, now_ms, tolerance_ms):
    """Ordered min-fold per (domain, type) plus precision findings."""
    by_key = {}
    for entry in entries:
        by_key.setdefault((entry.domain, entry.rtype), []).append(entry)
    comparisons = []
    findings = []
    for key in sorted(by_key):
        group = by_key[key]
        _, cmp_records = fold_min_expiry(group)
        comparisons.extend(cmp_records)
        conversions = [conversion_of(e) for e in group]
        findings.extend(analyze_comparisons(
            key[0], key[1], cmp_records, tolerance_ms, conversions))
        winner = min(group, key=lambda e: e.expiry_ms)
        findings.extend(analyze_expiry(
            winner, now_ms, tolerance_ms, conversions))
    return comparisons, findings


def op_load(store, now_us, tolerance_ms, v1_path=None, v2_dir=None):
    """Validate, merge and commit a snapshot set. Validation failures raise
    BizError before anything is persisted."""
    snapshots = []
    manifest = {"sources": []}
    v2 = None
    if v1_path:
        v1 = load_v1(v1_path)
        snapshots.append(v1)
        manifest["sources"].append({"format": "v1",
                                    "digest": _file_digest(v1_path)})
    if v2_dir:
        v2 = load_v2(v2_dir, _generation_high_water(store))
        snapshots.append(v2)
        manifest["sources"].append({
            "format": "v2", "version": v2.version,
            "manifest_digest": v2.manifest_digest,
            "shard_hashes": v2.shard_hashes,
            "min_generation": v2.min_generation})
    if not snapshots:
        raise BizError(E_NOT_FOUND, "no snapshot source given")
    current = store.current_snapshot_version()
    if v2 is not None:
        version = v2.version
    else:
        version = (current or 0) + 1
    if current is not None and version <= current:
        raise BizError(E_SNAPSHOT_VERSION_ROLLBACK,
                       "snapshot version must increase",
                       {"current": current, "requested": version})
    op_id = _op_id("load", {"v": version, "m": manifest, "now_us": now_us})
    entries, conversions = merge_snapshots(snapshots, now_us)
    for entry in entries:
        entry.snapshot_version = version
    comparisons, findings = _fold_and_findings(
        entries, now_us // 1000, tolerance_ms)
    ledger = conversions + comparisons
    manifest["version"] = version
    with store.write_lock() as conn:
        if store.journal_done_conn(conn, op_id):
            return {"op_id": op_id, "status": "already_committed",
                    "version": version}
        store.journal_begin(conn, op_id, "load",
                            {"v1_path": v1_path, "v2_dir": v2_dir,
                             "now_us": now_us, "tolerance_ms": tolerance_ms})
        store.commit_snapshot(conn, version, "load", manifest, now_us)
        if current is not None:
            store.delete_entries_of_snapshot(conn, current)
        store.upsert_entries(conn, entries)
        store.append_ledger(conn, op_id, ledger)
        store.upsert_findings(conn, findings)
        store.set_meta(conn, "current_snapshot_version", version)
        store.set_meta(conn, "clock_us", now_us)
        if v2 is not None:
            gens = [e["generation"] for e in v2.entries] or [0]
            high = max(_generation_high_water(store), max(gens),
                       v2.min_generation)
            store.set_meta(conn, "generation_high_water", high)
        store.journal_finish(conn, op_id)
    return {"op_id": op_id, "status": "committed", "version": version,
            "entries": len(entries), "ledger_records": len(ledger),
            "findings": len(findings)}


def _plan_replace_conn(conn, store, snap):
    """Classify an incoming v2 package vs the committed state. Must be
    called inside the writer transaction so the decision is race-free."""
    row = store.get_meta(conn, "current_snapshot_version")
    current = int(row) if row is not None else None
    if current is None:
        return "FULL_REPLACE", None
    if snap.version < current:
        raise BizError(E_SNAPSHOT_VERSION_ROLLBACK,
                       "snapshot version rollback rejected",
                       {"current": current, "requested": snap.version})
    srow = conn.execute("SELECT manifest_json FROM snapshots WHERE version=?",
                        (current,)).fetchone()
    current_manifest = json.loads(srow["manifest_json"]) if srow else {}
    current_sources = current_manifest.get("sources", [])
    current_v2 = next((s for s in current_sources
                       if s.get("format") == "v2"), {})
    if snap.version == current:
        if current_v2.get("manifest_digest") == snap.manifest_digest:
            return "NO_OP", current
        raise BizError(E_REPLACE_CONFLICT,
                       "same version with different manifest",
                       {"version": snap.version})
    high_water = int(store.get_meta(conn, "generation_high_water", 0))
    current_shards = set(current_v2.get("shard_hashes", {}).keys())
    new_shards = set(snap.shard_hashes.keys())
    if (snap.min_generation > high_water
            or not current_shards
            or not current_shards.issubset(new_shards)):
        return "FULL_REPLACE", current
    return "INCREMENTAL", current


def op_replace(store, v2_dir, now_us, tolerance_ms):
    """Apply a v2 snapshot package: NO_OP / INCREMENTAL / FULL_REPLACE.

    FULL_REPLACE switches the snapshot pointer atomically; INCREMENTAL bumps
    unaffected entries to the new version without touching their expiry or
    precision fields. Planning and commit happen inside one writer
    transaction, so a duplicate request is idempotent and a version can only
    be committed once.
    """
    snap = load_v2(v2_dir, 0)  # schema/hash validation before any persistence
    op_id = _op_id("replace", {"v": snap.version,
                               "d": snap.manifest_digest})
    with store.write_lock() as conn:
        if store.journal_done_conn(conn, op_id):
            arow = conn.execute(
                "SELECT action FROM replace_audit WHERE op_id=? "
                "ORDER BY id DESC LIMIT 1", (op_id,)).fetchone()
            action = arow["action"] if arow else "UNKNOWN"
            return {"op_id": op_id, "status": "already_committed",
                    "action": action, "version": snap.version}
        action, current = _plan_replace_conn(conn, store, snap)
        high_water = int(store.get_meta(conn, "generation_high_water", 0))
        snap = load_v2(v2_dir, high_water)  # generation monotonicity check
        store.journal_begin(conn, op_id, "replace",
                            {"v2_dir": v2_dir, "now_us": now_us,
                             "tolerance_ms": tolerance_ms})
        if action == "NO_OP":
            store.record_replace(conn, op_id, "NO_OP", current,
                                 snap.version,
                                 {"reason": "identical manifest"})
            store.journal_finish(conn, op_id)
            return {"op_id": op_id, "status": "committed", "action": "NO_OP",
                    "version": snap.version}
        entries, conversions = merge_snapshots([snap], now_us)
        for entry in entries:
            entry.snapshot_version = snap.version
        comparisons, findings = _fold_and_findings(
            entries, now_us // 1000, tolerance_ms)
        manifest = {"version": snap.version, "sources": [{
            "format": "v2", "version": snap.version,
            "manifest_digest": snap.manifest_digest,
            "shard_hashes": snap.shard_hashes,
            "min_generation": snap.min_generation}]}
        store.commit_snapshot(conn, snap.version, "replace", manifest,
                              now_us)
        if action == "FULL_REPLACE":
            if current is not None:
                store.delete_entries_of_snapshot(conn, current)
            store.upsert_entries(conn, entries)
            changed = len(entries)
        else:  # INCREMENTAL: bump unaffected entries, upsert only changes
            existing = {}
            for row in conn.execute(
                    "SELECT * FROM cache_entries WHERE snapshot_version=?",
                    (current,)):
                existing[(row["domain"], row["rtype"], row["level"])] = row
            conn.execute(
                "UPDATE cache_entries SET snapshot_version=? "
                "WHERE snapshot_version=?", (snap.version, current))
            to_write = []
            for entry in entries:
                old = existing.get(entry.key())
                if (old is None
                        or old["expiry_ms"] != entry.expiry_ms
                        or bool(old["negative"]) != entry.negative
                        or json.loads(old["records_json"]) != entry.records
                        or old["generation"] != entry.generation):
                    to_write.append(entry)
            store.upsert_entries(conn, to_write)
            changed = len(to_write)
        store.append_ledger(conn, op_id, conversions + comparisons)
        store.upsert_findings(conn, findings)
        store.set_meta(conn, "current_snapshot_version", snap.version)
        store.set_meta(conn, "clock_us", now_us)
        gens = [e["generation"] for e in snap.entries] or [0]
        store.set_meta(conn, "generation_high_water",
                       max(high_water, max(gens), snap.min_generation))
        store.record_replace(conn, op_id, action, current, snap.version,
                             {"changed_entries": changed,
                              "shard_hashes": snap.shard_hashes})
        store.journal_finish(conn, op_id)
        return {"op_id": op_id, "status": "committed", "action": action,
                "version": snap.version, "changed_entries": changed}


def _run_resolve_flow(store, name, rtype, now_us, tolerance_ms, upstream,
                      ledger_op_id):
    """Execute a resolve against the committed snapshot; returns
    (result, ledger, writes, findings). Pure w.r.t. the store."""
    pointer = store.current_snapshot_version()
    if pointer is None:
        raise BizError(E_NOT_FOUND, "no snapshot loaded")
    entries = store.load_entries(pointer)
    index = {e.key(): e for e in entries}
    session = ResolveSession(index, now_us, tolerance_ms, upstream, pointer)
    result = session.resolve(name, rtype)
    return result, session.ledger, session.writes, session.findings


def op_resolve(store, name, rtype, now_us, tolerance_ms, upstream=None):
    pointer = store.current_snapshot_version()
    if pointer is None:
        raise BizError(E_NOT_FOUND, "no snapshot loaded")
    op_id = _op_id("resolve", {"n": name.lower(), "t": rtype.upper(),
                               "now_us": now_us, "v": pointer})
    result, ledger, writes, findings = _run_resolve_flow(
        store, name, rtype, now_us, tolerance_ms, upstream, op_id)
    already = False
    with store.write_lock() as conn:
        if store.journal_done_conn(conn, op_id):
            already = True
        else:
            store.journal_begin(conn, op_id, "resolve",
                                {"name": name, "rtype": rtype,
                                 "now_us": now_us,
                                 "tolerance_ms": tolerance_ms})
            store.append_ledger(conn, op_id, ledger)
            store.upsert_entries(conn, writes)
            store.upsert_findings(conn, findings)
            store.set_meta(
                conn, "clock_us",
                max(int(store.get_meta(conn, "clock_us", 0)), now_us))
            store.journal_record_step(conn, op_id, "result", result)
            store.journal_finish(conn, op_id)
    if already:
        stored = store.journal_step_result(op_id, "result")
        stored["op_id"] = op_id
        stored["idempotent_replay"] = True
        return stored
    result = dict(result)
    result["op_id"] = op_id
    return result


def op_warmup(store, queries, clock_steps, tolerance_ms, upstream=None,
              crash_after_step=None):
    """Run a deterministic query template list, advancing the logical clock
    per step. Each step commits separately so resume replays only the
    unfinished suffix."""
    op_id = _op_id("warmup", {"q": queries, "c": clock_steps})
    if store.journal_done(op_id):
        return {"op_id": op_id, "status": "already_committed"}
    with store.write_lock() as conn:
        store.journal_begin(conn, op_id, "warmup",
                            {"queries": queries, "clock_steps": clock_steps,
                             "tolerance_ms": tolerance_ms})
    results = []
    for index, query in enumerate(queries):
        step_key = str(index)
        now_us = int(clock_steps[min(index, len(clock_steps) - 1)]["now_us"])
        ledger_op = f"{op_id}#{step_key}"
        result, ledger, writes, findings = _run_resolve_flow(
            store, query["name"], query["type"], now_us, tolerance_ms,
            upstream, ledger_op)
        committed = False
        with store.write_lock() as conn:
            stored = store.journal_step_result_conn(conn, op_id, step_key)
            if stored is not None:
                result = stored  # already committed by us or a peer
            else:
                store.append_ledger(conn, ledger_op, ledger)
                store.upsert_entries(conn, writes)
                store.upsert_findings(conn, findings)
                store.set_meta(
                    conn, "clock_us",
                    max(int(store.get_meta(conn, "clock_us", 0)), now_us))
                store.journal_record_step(conn, op_id, step_key, result)
                committed = True
        results.append(result)
        if committed and crash_after_step is not None \
                and index == crash_after_step:
            os._exit(3)  # simulated force kill, after the step commit
    with store.write_lock() as conn:
        store.journal_finish(conn, op_id)
    return {"op_id": op_id, "status": "committed", "results": results}


def op_resume(store, tolerance_ms, upstream=None):
    """Replay only the unfinished parts of interrupted operations."""
    replayed = []
    for op_id, kind, inputs in store.journal_pending():
        if kind == "warmup":
            op_warmup(store, inputs["queries"], inputs["clock_steps"],
                      inputs.get("tolerance_ms", tolerance_ms), upstream)
        elif kind == "resolve":
            op_resolve(store, inputs["name"], inputs["rtype"],
                       inputs["now_us"],
                       inputs.get("tolerance_ms", tolerance_ms), upstream)
        elif kind == "load":
            op_load(store, inputs["now_us"],
                    inputs.get("tolerance_ms", tolerance_ms),
                    v1_path=inputs.get("v1_path"),
                    v2_dir=inputs.get("v2_dir"))
        elif kind == "replace":
            op_replace(store, inputs["v2_dir"], inputs["now_us"],
                       inputs.get("tolerance_ms", tolerance_ms))
        else:
            continue
        replayed.append(op_id)
    return {"replayed": replayed}


def op_diagnose(store):
    """Stable-sorted diagnostic JSON: precision-sensitive entries, error
    source chains and flip tracing."""
    findings = store.findings_all()
    doc = {
        "findings": findings,
        "finding_count": len(findings),
        "ledger_entries": len(store.ledger_dump()),
        "current_snapshot_version": store.current_snapshot_version(),
        "clock_us": store.get_clock_us(),
    }
    return doc


def op_status(store):
    conn = store.connect()
    try:
        ledger_count = conn.execute(
            "SELECT COUNT(*) AS c FROM ledger").fetchone()["c"]
        findings_count = conn.execute(
            "SELECT COUNT(*) AS c FROM findings").fetchone()["c"]
        snapshots = [dict(r) for r in conn.execute(
            "SELECT version,kind,committed_at_us FROM snapshots "
            "ORDER BY version")]
    finally:
        conn.close()
    return {
        "current_snapshot_version": store.current_snapshot_version(),
        "clock_us": store.get_clock_us(),
        "generation_high_water": _generation_high_water(store),
        "ledger_entries": ledger_count,
        "findings": findings_count,
        "snapshots": snapshots,
        "pending_ops": [op_id for op_id, _, _ in store.journal_pending()],
    }


def op_verify(store):
    """Consistency checks over the persisted state."""
    checks = []
    ledger = store.ledger_dump()
    seqs = [row["seq"] for row in ledger]
    checks.append({"check": "ledger_seq_continuous",
                   "ok": seqs == list(range(len(seqs)))})
    pointer = store.current_snapshot_version()
    checks.append({"check": "pointer_exists",
                   "ok": pointer is None
                   or store.snapshot_info(pointer) is not None})
    entries = store.load_entries()
    stray = [e for e in entries
             if pointer is not None and e.snapshot_version != pointer]
    checks.append({"check": "entries_match_pointer", "ok": not stray,
                   "stray": len(stray)})
    bad_bounds = []
    for entry in entries:
        conv = conversion_of(entry)
        if abs(conv.error_bound_ms - entry.error_bound_ms) > 1e-9:
            bad_bounds.append(entry.key())
    checks.append({"check": "error_bounds_recompute",
                   "ok": not bad_bounds,
                   "mismatches": [list(k) for k in bad_bounds]})
    pending = store.journal_pending()
    checks.append({"check": "no_pending_ops", "ok": not pending,
                   "pending": [p[0] for p in pending]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
