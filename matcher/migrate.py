"""Stage 4 support: bidirectional v1 <-> v2 ledger migration."""
import json
import os
import sqlite3

from .errors import BusinessError, E_MIGRATION_MISMATCH
from .ledger import read_v1, read_v2, write_v1, write_v2
from .models import MatchRow, ImageMatch, canonical_json, content_hash, norm_float, write_atomic

DISCARD_RULE = "drop-v2-only-fields"
V2_ONLY_FIELDS = ("t_abs_ms", "generation", "cand_hash", "gt_revision", "row_hash")


def _rows_to_matches(rows):
    """Rebuild per-image ImageMatch shells (rows only) from ledger row dicts."""
    by_image = {}
    for r in rows:
        by_image.setdefault(r["image_id"], []).append(r)
    matches = []
    for image_id in sorted(by_image):
        im = ImageMatch(image_id=image_id)
        for r in by_image[image_id]:
            im.rows.append(
                MatchRow(
                    image_id=image_id,
                    cand_idx=r["cand_idx"],
                    gt_idx=r["gt_idx"],
                    status=r["status"],
                    iou=norm_float(r["iou"]),
                    inter=norm_float(r.get("inter", 0.0)),
                    union=norm_float(r.get("union", 0.0)),
                    class_id=r["class_id"],
                    score=r.get("score"),
                    cand_hash=r.get("cand_hash"),
                    gt_revision=r.get("gt_revision"),
                )
            )
        matches.append(im)
    return matches


def status_multiset(rows):
    """TP/FP/FN assignment multiset per image, used for equivalence checks."""
    result = {}
    for r in rows:
        key = (r["image_id"], r["cand_idx"], r["gt_idx"], r["status"])
        result[key] = result.get(key, 0) + 1
    return result


def verify_migration(rows_a, rows_b, agg_a, agg_b):
    if status_multiset(rows_a) != status_multiset(rows_b):
        raise BusinessError(
            E_MIGRATION_MISMATCH,
            "TP/FP/FN assignment sets differ between ledger generations",
            field="rows",
        )
    if canonical_json(agg_a) != canonical_json(agg_b):
        raise BusinessError(
            E_MIGRATION_MISMATCH,
            "aggregate scores differ between ledger generations",
            field="aggregate",
            context={"a": agg_a, "b": agg_b},
        )


def migrate_v1_to_v2(v1_path, v2_dir, cfg, ledger_version, candidate_store=None):
    """Upgrade v1 -> v2, filling generation/hash/revision defaults with provenance."""
    doc = read_v1(v1_path)
    provenance = []
    rows = []
    for r in doc["rows"]:
        row_key = "%s/%s/%s/%s" % (r["image_id"], r["cand_idx"], r["gt_idx"], r["status"])
        cand_hash = None
        if r["cand_idx"] is not None:
            cand = (candidate_store or {}).get((r["image_id"], r["cand_idx"]))
            if cand is not None:
                cand_hash = cand.hash()
                provenance.append((row_key, "cand_hash", "computed:candidate-content"))
            else:
                cand_hash = content_hash(
                    {"image_id": r["image_id"], "cand_idx": r["cand_idx"],
                     "class_id": r["class_id"], "score": r["score"]}
                )
                provenance.append((row_key, "cand_hash", "default:row-content"))
        gt_revision = None
        if r["gt_idx"] is not None:
            gt_revision = 0
            provenance.append((row_key, "gt_revision", "default:rev0"))
        provenance.append((row_key, "generation", "default:gen0"))
        provenance.append((row_key, "t_abs_ms", "derived:relative-clock"))
        rows.append(
            {
                "image_id": r["image_id"],
                "cand_idx": r["cand_idx"],
                "gt_idx": r["gt_idx"],
                "status": r["status"],
                "iou": r["iou"],
                "inter": r.get("inter", 0.0),
                "union": r.get("union", 0.0),
                "class_id": r["class_id"],
                "score": r.get("score"),
                "cand_hash": cand_hash,
                "gt_revision": gt_revision,
                "t_abs_ms": cfg.clock_base_ms
                + int(round(norm_float(r["t_rel_s"]) * 1000)),
                "generation": 0,
            }
        )
    matches = _rows_to_matches(rows)
    generations = {im.image_id: 0 for im in matches}
    result = write_v2(v2_dir, matches, generations, cfg, ledger_version)
    conn = sqlite3.connect(os.path.join(v2_dir, "meta.sqlite3"))
    try:
        conn.executemany(
            "INSERT INTO migration_log (row_key, field, source) VALUES (?,?,?)",
            provenance,
        )
        conn.commit()
    finally:
        conn.close()
    verify_migration(doc["rows"], result["rows"], doc["aggregate"],
                     _aggregate_of(result["rows"]))
    return {"provenance": provenance, "rows": len(result["rows"])}


def _aggregate_of(rows):
    matches = _rows_to_matches(rows)
    from .match import aggregate_all

    return aggregate_all(matches)


def migrate_v2_to_v1(v2_dir, v1_path, downgrade_log_path, cfg, ledger_version):
    """Downgrade v2 -> v1, explicitly discarding v2-only fields with a record."""
    data = read_v2(v2_dir)
    rows = data["rows"]
    matches = _rows_to_matches(rows)
    doc_v1 = write_v1(v1_path, matches, cfg, ledger_version)
    record = {
        "from": "v2",
        "to": "v1",
        "rule": DISCARD_RULE,
        "discarded_fields": list(V2_ONLY_FIELDS),
        "rows_affected": len(rows),
        "ledger_version": int(ledger_version),
    }
    write_atomic(downgrade_log_path, canonical_json(record))
    verify_migration(rows, doc_v1["rows"], _aggregate_of(rows), doc_v1["aggregate"])
    return record
