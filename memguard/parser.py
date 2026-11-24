"""Strict parsing / normalization layer for declarations and traces.

Rules:
  * integers only (bool and float are rejected, never coerced);
  * a bad record yields a Reject(file_id, line_no, code) and parsing
    continues with the remaining records;
  * a wholesale-unreadable file yields a single line-0 reject and never
    aborts sibling inputs.
"""

from __future__ import annotations

import json
import os

from . import codes
from .model import ParseOutcome, RegionDecl, Reject, TraceRecord

ACCESS_KINDS = ("read", "write")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _req_int(obj, name, errs, minimum=None, maximum=None):
    value = obj.get(name)
    if not _is_int(value):
        errs.append(f"{name}: not an integer")
        return None
    if minimum is not None and value < minimum:
        errs.append(f"{name}: below minimum {minimum}")
        return None
    if maximum is not None and value > maximum:
        errs.append(f"{name}: above maximum {maximum}")
        return None
    return value


def _req_str(obj, name, errs):
    value = obj.get(name)
    if not isinstance(value, str) or not value:
        errs.append(f"{name}: not a non-empty string")
        return None
    return value


def _opt_int(obj, name, errs, minimum=None):
    value = obj.get(name)
    if value is None:
        return None
    return _req_int(obj, name, errs, minimum=minimum)


def file_id_for(path: str) -> str:
    return os.path.basename(path)


def parse_declarations(path: str) -> ParseOutcome:
    """Parse a declarations JSON document: {"regions": [...]}."""
    outcome = ParseOutcome()
    fid = file_id_for(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        outcome.rejects.append(Reject(fid, 0, codes.E_JSON_LINE, f"unreadable declarations: {exc}"))
        return outcome
    regions = doc.get("regions") if isinstance(doc, dict) else None
    if not isinstance(regions, list):
        outcome.rejects.append(Reject(fid, 0, codes.E_SCHEMA_FIELD, "missing 'regions' array"))
        return outcome

    seen_ids = set()
    for idx, raw in enumerate(regions, start=1):
        if not isinstance(raw, dict):
            outcome.rejects.append(Reject(fid, idx, codes.E_SCHEMA_FIELD, "region entry not an object"))
            continue
        errs = []
        region_id = _req_str(raw, "region_id", errs)
        generation = _req_int(raw, "generation", errs, minimum=0)
        tombstone = raw.get("tombstone", False)
        if not isinstance(tombstone, bool):
            errs.append("tombstone: not a boolean")
            tombstone = False
        arena_size = _req_int(raw, "arena_size", errs, minimum=1)
        start = _req_int(raw, "start", errs, minimum=0)
        end = _req_int(raw, "end", errs, minimum=0)
        life_start = _req_int(raw, "life_start_ms", errs, minimum=0)
        life_end = _opt_int(raw, "life_end_ms", errs, minimum=0)
        parent_id = raw.get("parent_id")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
            errs.append("parent_id: not a non-empty string or null")
            parent_id = None
        if errs:
            outcome.rejects.append(Reject(fid, idx, codes.E_SCHEMA_FIELD, "; ".join(errs)))
            continue
        if end < start:
            outcome.rejects.append(Reject(fid, idx, codes.E_SCHEMA_FIELD, "end < start"))
            continue
        if end > arena_size:
            outcome.rejects.append(Reject(fid, idx, codes.E_SCHEMA_FIELD, "end > arena_size"))
            continue
        if life_end is not None and life_end < life_start:
            outcome.rejects.append(Reject(fid, idx, codes.E_SCHEMA_FIELD, "life_end_ms < life_start_ms"))
            continue
        if region_id in seen_ids:
            outcome.rejects.append(Reject(fid, idx, codes.E_DUP_REGION, f"duplicate region_id {region_id!r} in file"))
            continue
        if parent_id == region_id:
            outcome.rejects.append(Reject(fid, idx, codes.E_REGION_CYCLE, f"self parent {region_id!r}"))
            continue
        seen_ids.add(region_id)
        outcome.decls.append(RegionDecl(
            region_id=region_id, generation=generation, tombstone=tombstone,
            arena_size=arena_size, start=start, end=end,
            life_start_ms=life_start, life_end_ms=life_end,
            parent_id=parent_id, file_id=fid, line_no=idx,
        ))
    return outcome


def parse_traces(path: str) -> ParseOutcome:
    """Parse a trace JSONL file, one access record per line."""
    outcome = ParseOutcome()
    fid = file_id_for(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        outcome.rejects.append(Reject(fid, 0, codes.E_JSON_LINE, f"unreadable trace file: {exc}"))
        return outcome

    seen_ids = set()
    for line_no, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            outcome.rejects.append(Reject(fid, line_no, codes.E_JSON_LINE, str(exc)))
            continue
        if not isinstance(raw, dict):
            outcome.rejects.append(Reject(fid, line_no, codes.E_SCHEMA_FIELD, "record not an object"))
            continue
        errs = []
        trace_id = _req_str(raw, "trace_id", errs)
        access_seq = _req_int(raw, "access_seq", errs, minimum=0)
        region_id = _req_str(raw, "region_id", errs)
        arena_size = _req_int(raw, "arena_size", errs, minimum=1)
        base_offset = _req_int(raw, "base_offset", errs, minimum=0)
        access_size = _req_int(raw, "access_size", errs, minimum=0)
        ts_ms = _req_int(raw, "ts_ms", errs, minimum=0)
        kind = raw.get("access_kind")
        if kind not in ACCESS_KINDS:
            outcome.rejects.append(Reject(fid, line_no, codes.E_KIND_UNKNOWN, f"access_kind {kind!r}"))
            continue
        if errs:
            outcome.rejects.append(Reject(fid, line_no, codes.E_SCHEMA_FIELD, "; ".join(errs)))
            continue
        if base_offset > arena_size:
            outcome.rejects.append(Reject(fid, line_no, codes.E_SCHEMA_FIELD, "base_offset > arena_size"))
            continue
        if trace_id in seen_ids:
            outcome.rejects.append(Reject(fid, line_no, codes.E_DUP_TRACE, f"duplicate trace_id {trace_id!r} in file"))
            continue
        seen_ids.add(trace_id)
        outcome.traces.append(TraceRecord(
            trace_id=trace_id, access_seq=access_seq, region_id=region_id,
            base_offset=base_offset, access_size=access_size, access_kind=kind,
            arena_size=arena_size, ts_ms=ts_ms, file_id=fid, line_no=line_no,
        ))
    return outcome
