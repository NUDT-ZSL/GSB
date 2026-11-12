"""Per-document analysis pipeline: format detection, bounded-concurrency
deep parsing with real timeout interruption, dedup and aggregation."""

import hashlib
import io
import multiprocessing as mp
import os
import queue as queue_mod
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import olefile

from . import deepworker
from .rules import match_rules

CFB_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")

PARTIAL_FAILURE_STATUSES = ("parse_error", "timeout", "size_exceeded")

MAX_WORKERS_LIMIT = 8


class ConcurrencyCounter:
    """Tracks the number of deep parses currently in flight."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def acquire(self):
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def release(self):
        with self._lock:
            self.current -= 1


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sniff_format(path: str) -> str:
    """Cheap format sniff from the first bytes (used for oversized files)."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(CFB_SIGNATURE):
        return "ole"
    if head[:2] == b"PK":
        return "ooxml"
    return "unknown"


def detect_format(data: bytes) -> str:
    """Full format detection: 'ooxml', 'ole' or 'unknown'."""
    if data[:8] == CFB_SIGNATURE:
        return "ole"
    if zipfile.is_zipfile(io.BytesIO(data)):
        return "ooxml"
    return "unknown"


def _new_record(index: int, path: str) -> dict:
    return {
        "id": "doc%03d" % (index + 1),
        "path": os.path.abspath(path),
        "format": "unknown",
        "status": "pending",
        "input_sha256": None,
        "size_bytes": 0,
        "analysis_ms": 0.0,
        "truncated": False,
        "modules": [],
        "features": [],
        "matched_rules": [],
        "confidence": 0.0,
        "errors": [],
        "duplicate_of": None,
        "payload_fingerprint": None,
    }


def _prescan(rec: dict, max_bytes: int):
    """Format detection and macro-stream location, before scheduling.

    Returns a deep-parse job tuple (container_bytes) or None."""
    path = rec["path"]
    rec["size_bytes"] = os.path.getsize(path)
    rec["input_sha256"] = sha256_file(path)
    if rec["size_bytes"] > max_bytes:
        rec["format"] = sniff_format(path)
        rec["status"] = "size_exceeded"
        rec["errors"].append(
            "size %d exceeds limit %d; deep parse skipped"
            % (rec["size_bytes"], max_bytes))
        return None
    with open(path, "rb") as fh:
        data = fh.read()
    fmt = detect_format(data)
    rec["format"] = fmt
    if fmt == "unknown":
        rec["status"] = "parse_error"
        rec["errors"].append("unrecognized or corrupt container format")
        return None
    if fmt == "ooxml":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "word/vbaProject.bin" not in names:
                    rec["status"] = "no_macro"
                    return None
                return zf.read("word/vbaProject.bin")
        except Exception as exc:
            rec["status"] = "parse_error"
            rec["errors"].append("corrupt OOXML container: %s" % exc)
            return None
    # fmt == "ole"
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
        try:
            streams = {"/".join(p) for p in ole.listdir()}
        finally:
            ole.close()
    except Exception as exc:
        rec["status"] = "parse_error"
        rec["errors"].append("corrupt OLE container: %s" % exc)
        return None
    if not any(s.endswith("VBA/dir") or s == "VBA/dir" for s in streams):
        rec["status"] = "no_macro"
        return None
    return data


def _run_deep_parse(container_bytes: bytes, timeout_ms: int,
                    counter: ConcurrencyCounter) -> dict:
    """Run the deep parse in a child process with a hard timeout.

    The timeout genuinely terminates the child process; module/feature
    messages already received are kept as partial results. The deadline
    starts when the child signals it has begun parsing, so interpreter
    startup time is not counted against the parse budget.
    """
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=deepworker.entry,
                       args=(container_bytes, None, q))
    counter.acquire()
    proc.start()
    modules, features, errors = [], [], []
    status, truncated = "analyzed", False
    deadline = None
    try:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                status, truncated = "timeout", True
                errors.append(
                    "deep parse timeout after %d ms; partial results retained"
                    % timeout_ms)
                break
            wait = 0.02 if deadline is None else max(
                0.0, min(0.02, deadline - now))
            try:
                msg = q.get(timeout=wait)
            except queue_mod.Empty:
                if not proc.is_alive():
                    # give the queue feeder a last chance to flush
                    try:
                        msg = q.get(timeout=0.5)
                    except queue_mod.Empty:
                        if not modules and status == "analyzed":
                            status = "parse_error"
                            errors.append("worker exited without a result")
                        break
                else:
                    continue
            mtype = msg.get("type")
            if mtype == "started":
                deadline = time.monotonic() + timeout_ms / 1000.0
            elif mtype == "module":
                modules.append(msg["module"])
                features.extend(msg["features"])
            elif mtype == "done":
                errors.extend(msg.get("errors", []))
                break
            elif mtype == "fatal":
                status = "parse_error"
                errors.append(msg["message"])
                break
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(timeout=5)
        counter.release()
    return {"status": status, "truncated": truncated, "modules": modules,
            "features": features, "errors": errors}


def payload_fingerprint(modules) -> str:
    """SHA-256 over the normalised module sources (deterministic)."""
    h = hashlib.sha256()
    for mod in modules:
        h.update(mod["name"].encode("utf-8"))
        h.update(b"\x01")
        for stmt in mod.get("normalized", []):
            h.update(stmt.encode("utf-8"))
            h.update(b"\x00")
        h.update(b"\x02")
    return h.hexdigest()


def analyze_documents(paths, ruleset, max_bytes, timeout_ms, max_workers):
    """Analyze *paths* in input order. Returns (documents, concurrency_peak)."""
    max_workers = max(1, min(max_workers, MAX_WORKERS_LIMIT))
    records = [_new_record(i, p) for i, p in enumerate(paths)]
    deep_jobs = {}  # index -> container bytes

    # Phase 1: format detection before any scheduling.
    for i, rec in enumerate(records):
        t0 = time.monotonic()
        container = _prescan(rec, max_bytes)
        rec["analysis_ms"] = round((time.monotonic() - t0) * 1000.0, 3)
        if container is not None:
            deep_jobs[i] = container

    # Phase 2: bounded-concurrency deep parsing with per-document timeout.
    counter = ConcurrencyCounter()

    def work(idx):
        rec = records[idx]
        t0 = time.monotonic()
        result = _run_deep_parse(deep_jobs[idx], timeout_ms, counter)
        rec["status"] = result["status"]
        rec["truncated"] = result["truncated"]
        rec["modules"] = result["modules"]
        rec["features"] = result["features"]
        rec["errors"].extend(result["errors"])
        rec["analysis_ms"] = round(rec["analysis_ms"]
                                   + (time.monotonic() - t0) * 1000.0, 3)

    if deep_jobs:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(work, sorted(deep_jobs)))

    # Phase 3: rule matching (parent side, uses the loaded ruleset).
    for rec in records:
        if rec["status"] in ("analyzed", "timeout"):
            hits, confidence = match_rules(
                ruleset, [f["type"] for f in rec["features"]])
            rec["matched_rules"] = hits
            rec["confidence"] = confidence

    # Phase 4: deterministic dedup on normalised module sources.
    for rec in records:
        if rec["status"] == "analyzed":
            rec["payload_fingerprint"] = payload_fingerprint(rec["modules"])
    canonical = {}
    for rec in records:  # input order: first doc of a group is canonical
        fp = rec["payload_fingerprint"]
        if fp is None:
            continue
        if fp in canonical:
            rec["duplicate_of"] = canonical[fp]
        else:
            canonical[fp] = rec["id"]

    return records, counter.peak, max_workers


def build_groups(records):
    """Group analyzed documents by payload fingerprint, sorted by fingerprint."""
    groups = {}
    for rec in records:
        fp = rec["payload_fingerprint"]
        if fp is None:
            continue
        groups.setdefault(fp, []).append(rec)
    out = []
    for fp in sorted(groups):
        members = groups[fp]
        rule_hits = {}
        for rec in members:
            for hit in rec["matched_rules"]:
                rule_hits[hit["id"]] = rule_hits.get(hit["id"], 0) + 1
        out.append({
            "fingerprint": fp,
            "canonical": members[0]["id"],
            "members": [r["id"] for r in members],
            "rule_hits": dict(sorted(rule_hits.items())),
        })
    return out


def build_aggregate(records, groups):
    rule_hits = {}
    for rec in records:
        for hit in rec["matched_rules"]:
            rule_hits[hit["id"]] = rule_hits.get(hit["id"], 0) + 1
    return {
        "unique_payloads": len(groups),
        "duplicates": sum(len(g["members"]) - 1 for g in groups),
        "rule_hits": dict(sorted(rule_hits.items())),
        "by_fingerprint": groups,
    }
