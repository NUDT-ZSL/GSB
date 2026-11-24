"""Diagnostic report and per-region query rendering."""

from __future__ import annotations

from . import REPORT_SCHEMA_VERSION, codes, store


def _region_report(conn, region_id):
    kinds = {}
    for row in conn.execute(
            "SELECT access_kind, total_count, oob_count, oob_max_end, watermark,"
            " dup_count FROM ledger WHERE region_id=? ORDER BY access_kind",
            (region_id,)):
        kind, total, oob, oob_max_end, watermark, dup = row
        evidence = [
            {"trace_id": e[0], "access_seq": e[1], "code": e[2],
             "base_offset": e[3], "access_size": e[4]}
            for e in conn.execute(
                "SELECT trace_id, access_seq, code, base_offset, access_size"
                " FROM evidence WHERE region_id=? AND access_kind=?"
                " ORDER BY access_seq, trace_id", (region_id, kind))
        ]
        gaps = store.gaps_for(conn, region_id, kind)
        entry_codes = sorted({e["code"] for e in evidence})
        if gaps:
            entry_codes.append(codes.E_SEQ_GAP)
        kinds[kind] = {
            "total_count": total,
            "oob_count": oob,
            "oob_max_end": oob_max_end,
            "watermark": watermark,
            "dup_count": dup,
            "codes": entry_codes,
            "gaps": gaps,
            "evidence": evidence,
        }
    all_codes = sorted({c for k in kinds.values() for c in k["codes"]})
    verdict = "VIOLATION" if any(c in codes.VIOLATION_CODES for c in all_codes) else "CLEAN"
    return {
        "region_id": region_id,
        "verdict": verdict,
        "codes": all_codes,
        "kinds": kinds,
    }


def query_region(workdir, region_id):
    from .engine import db_path
    conn = store.connect(db_path(workdir))
    try:
        regions = store.load_regions(conn)
        if region_id not in regions:
            return {"region_id": region_id, "verdict": "UNKNOWN_REGION", "codes": []}
        return _region_report(conn, region_id)
    finally:
        conn.close()


def build_report(workdir):
    from .engine import db_path
    conn = store.connect(db_path(workdir))
    try:
        regions = store.load_regions(conn)
        region_reports = [_region_report(conn, rid) for rid in sorted(regions)]
        rejects = [
            {"file_id": r[0], "line_no": r[1], "code": r[2], "detail": r[3]}
            for r in conn.execute(
                "SELECT file_id, line_no, code, detail FROM rejects"
                " ORDER BY file_id, line_no, code")
        ]
        ckpt = conn.execute(
            "SELECT batch_seq, digest FROM checkpoints ORDER BY batch_seq DESC LIMIT 1"
        ).fetchone()
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "workdir": workdir,
            "batch_watermark": store.batch_watermark(conn),
            "checkpoint": ({"batch_seq": ckpt[0], "ledger_digest": ckpt[1]}
                           if ckpt else None),
            "regions": region_reports,
            "rejects": rejects,
        }
    finally:
        conn.close()
