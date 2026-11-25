"""Stage 3: dual-format match ledger (v1 single JSON, v2 shards + SQLite)."""
import hashlib
import json
import os
import sqlite3

from .errors import (
    BusinessError,
    E_FORMAT_CORRUPT,
    E_VERSION_UNSUPPORTED,
)
from .match import aggregate_all
from .models import canonical_json, content_hash, norm_float, write_atomic

FORMAT_TAG = "match-ledger"
SHARD_TAG = "match-ledger-shard"
SUPPORTED_VERSIONS = ("v1", "v2")

V1_ROW_FIELDS = (
    "image_id",
    "t_rel_s",
    "cand_idx",
    "gt_idx",
    "status",
    "iou",
    "inter",
    "union",
    "class_id",
    "score",
)
V2_ROW_FIELDS = tuple(f for f in V1_ROW_FIELDS if f != "t_rel_s") + (
    "t_abs_ms",
    "generation",
    "cand_hash",
    "gt_revision",
    "row_hash",
)


def _shard_of(image_id, n_shards):
    digest = hashlib.sha256(image_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % n_shards


def _base_row(row, t_rel_s):
    return {
        "image_id": row.image_id,
        "t_rel_s": norm_float(t_rel_s),
        "cand_idx": row.cand_idx,
        "gt_idx": row.gt_idx,
        "status": row.status,
        "iou": norm_float(row.iou),
        "inter": norm_float(row.inter),
        "union": norm_float(row.union),
        "class_id": row.class_id,
        "score": norm_float(row.score) if row.score is not None else None,
    }


def v2_row(row, t_abs_ms, generation):
    doc = _base_row(row, 0.0)
    del doc["t_rel_s"]
    doc.update(
        {
            "t_abs_ms": int(t_abs_ms),
            "generation": int(generation),
            "cand_hash": row.cand_hash,
            "gt_revision": row.gt_revision,
        }
    )
    doc["row_hash"] = content_hash(doc)
    return doc


def write_v1(path, matches, cfg, ledger_version):
    """Write the v1 single-file ledger (flat rows, relative submit seconds)."""
    rows = []
    for seq, im in enumerate(sorted(matches, key=lambda m: m.image_id)):
        t_rel_s = norm_float(seq * cfg.clock_step_ms / 1000.0)
        for row in im.rows:
            rows.append(_base_row(row, t_rel_s))
    doc = {
        "format": FORMAT_TAG,
        "version": "v1",
        "ledger_version": int(ledger_version),
        "config": {"iou_threshold": norm_float(cfg.iou_threshold)},
        "clock": {"base_ms": cfg.clock_base_ms, "step_ms": cfg.clock_step_ms},
        "rows": rows,
        "aggregate": aggregate_all(list(matches)),
    }
    doc["checksum"] = content_hash(doc["rows"])
    write_atomic(path, canonical_json(doc))
    return doc


def write_v2(v2_dir, matches, generations, cfg, ledger_version):
    """Write the v2 sharded ledger plus its SQLite meta database."""
    shards_dir = os.path.join(v2_dir, "shards")
    os.makedirs(shards_dir, exist_ok=True)
    degraded = []
    n_shards = cfg.n_shards
    ordered = sorted(matches, key=lambda m: m.image_id)
    needed = {_shard_of(im.image_id, n_shards) for im in ordered}
    if len(needed) > cfg.max_open_shards:
        if not cfg.allow_degrade:
            from .errors import E_RESOURCE_LIMIT

            raise BusinessError(
                E_RESOURCE_LIMIT,
                "open shard count %d exceeds limit %d"
                % (len(needed), cfg.max_open_shards),
                field="max_open_shards",
            )
        n_shards = max(1, min(cfg.max_open_shards, n_shards))
        degraded.append("open_shards")
    shard_rows = {}
    all_rows = []
    for seq, im in enumerate(ordered):
        t_abs_ms = cfg.clock_base_ms + seq * cfg.clock_step_ms
        generation = generations.get(im.image_id, 0)
        shard = _shard_of(im.image_id, n_shards)
        for row in im.rows:
            doc = v2_row(row, t_abs_ms, generation)
            shard_rows.setdefault(shard, []).append(doc)
            all_rows.append(doc)
    shard_checksums = {}
    for shard in sorted(shard_rows):
        doc = {
            "format": SHARD_TAG,
            "version": "v2",
            "shard": shard,
            "n_shards": n_shards,
            "rows": shard_rows[shard],
        }
        text = canonical_json(doc)
        write_atomic(os.path.join(shards_dir, "shard_%02d.json" % shard), text)
        shard_checksums[str(shard)] = content_hash(shard_rows[shard])
    # drop stale shard files from a previous wider sharding
    for name in os.listdir(shards_dir):
        if name.startswith("shard_") and name.endswith(".json"):
            idx = int(name[len("shard_") : -len(".json")])
            if idx not in shard_rows:
                os.remove(os.path.join(shards_dir, name))
    db_path = os.path.join(v2_dir, "meta.sqlite3")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cur.execute(
            "CREATE TABLE rows ("
            "image_id TEXT NOT NULL, cand_idx INTEGER, gt_idx INTEGER, "
            "status TEXT NOT NULL, iou REAL NOT NULL, class_id INTEGER NOT NULL, "
            "t_abs_ms INTEGER NOT NULL, generation INTEGER NOT NULL, "
            "cand_hash TEXT, gt_revision INTEGER, shard INTEGER NOT NULL, "
            "row_hash TEXT NOT NULL)"
        )
        cur.execute(
            "CREATE TABLE migration_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, row_key TEXT NOT NULL, "
            "field TEXT NOT NULL, source TEXT NOT NULL)"
        )
        meta = {
            "format": FORMAT_TAG,
            "version": "v2",
            "ledger_version": str(int(ledger_version)),
            "n_shards": str(n_shards),
            "iou_threshold": repr(norm_float(cfg.iou_threshold)),
            "clock_base_ms": str(cfg.clock_base_ms),
            "clock_step_ms": str(cfg.clock_step_ms),
            "shard_checksums": canonical_json(shard_checksums),
            "row_count": str(len(all_rows)),
            "aggregate": canonical_json(aggregate_all(list(matches))),
            "degraded": canonical_json(degraded),
        }
        cur.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
        for doc in all_rows:
            cur.execute(
                "INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc["image_id"],
                    doc["cand_idx"],
                    doc["gt_idx"],
                    doc["status"],
                    doc["iou"],
                    doc["class_id"],
                    doc["t_abs_ms"],
                    doc["generation"],
                    doc["cand_hash"],
                    doc["gt_revision"],
                    _shard_of(doc["image_id"], n_shards),
                    doc["row_hash"],
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return {"rows": all_rows, "degraded": degraded, "n_shards": n_shards}


def _validate_row(doc, fields, where):
    for name in fields:
        if name not in doc:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "ledger row missing field %r at %s" % (name, where),
                field=name,
            )
    for name in ("t_rel_s", "iou", "inter", "union"):
        if name in doc and (
            isinstance(doc[name], bool) or not isinstance(doc[name], (int, float))
        ):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "ledger field %r not a number at %s" % (name, where),
                field=name,
            )
    if doc["status"] not in ("TP", "FP", "FN"):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "ledger row has invalid status %r at %s" % (doc["status"], where),
            field="status",
        )


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT, "unparseable ledger file %s" % path, field="file"
        ) from exc


def read_v1(path):
    doc = _load_json(path)
    if not isinstance(doc, dict) or doc.get("format") != FORMAT_TAG:
        raise BusinessError(
            E_FORMAT_CORRUPT, "bad v1 ledger header in %s" % path, field="format"
        )
    if doc.get("version") != "v1":
        raise BusinessError(
            E_VERSION_UNSUPPORTED,
            "unsupported ledger version %r" % doc.get("version"),
            field="version",
        )
    rows = doc.get("rows")
    if not isinstance(rows, list):
        raise BusinessError(
            E_FORMAT_CORRUPT, "v1 ledger rows missing in %s" % path, field="rows"
        )
    for i, row in enumerate(rows):
        _validate_row(row, V1_ROW_FIELDS, "%s#row[%d]" % (path, i))
    return doc


def read_v2(v2_dir, verify_shards=True):
    db_path = os.path.join(v2_dir, "meta.sqlite3")
    if not os.path.exists(db_path):
        raise BusinessError(
            E_FORMAT_CORRUPT, "v2 meta database missing at %s" % db_path, field="file"
        )
    try:
        conn = sqlite3.connect(db_path)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            raw_rows = conn.execute(
                "SELECT image_id, cand_idx, gt_idx, status, iou, class_id, "
                "t_abs_ms, generation, cand_hash, gt_revision, shard, row_hash "
                "FROM rows ORDER BY image_id, rowid"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT, "corrupt v2 meta database: %s" % exc, field="file"
        ) from exc
    if meta.get("format") != FORMAT_TAG:
        raise BusinessError(
            E_FORMAT_CORRUPT, "bad v2 meta header", field="format"
        )
    if meta.get("version") != "v2":
        raise BusinessError(
            E_VERSION_UNSUPPORTED,
            "unsupported ledger version %r" % meta.get("version"),
            field="version",
        )
    for rec in raw_rows:
        doc = {
            "image_id": rec[0],
            "cand_idx": rec[1],
            "gt_idx": rec[2],
            "status": rec[3],
            "iou": norm_float(rec[4]),
            "class_id": rec[5],
            "t_abs_ms": rec[6],
            "generation": rec[7],
            "cand_hash": rec[8],
            "gt_revision": rec[9],
            "shard": rec[10],
            "row_hash": rec[11],
        }
        _validate_row(doc, ("image_id", "status", "iou", "t_abs_ms"), "v2.meta")
    n_shards = int(meta["n_shards"])
    rows = []
    checksums = json.loads(meta.get("shard_checksums", "{}"))
    for shard_s, expected in sorted(checksums.items()):
        shard_path = os.path.join(
            v2_dir, "shards", "shard_%02d.json" % int(shard_s)
        )
        doc = _load_json(shard_path)
        if doc.get("format") != SHARD_TAG or doc.get("version") != "v2":
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "bad shard header in %s" % shard_path,
                field="format",
            )
        shard_rows = doc.get("rows")
        if not isinstance(shard_rows, list):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "shard rows missing in %s" % shard_path,
                field="rows",
            )
        for i, row in enumerate(shard_rows):
            _validate_row(row, V2_ROW_FIELDS, "%s#row[%d]" % (shard_path, i))
        if verify_shards and content_hash(shard_rows) != expected:
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "shard %s checksum mismatch" % shard_path,
                field="checksum",
            )
        rows.extend(shard_rows)
    if int(meta.get("row_count", "-1")) != len(rows):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "v2 row count mismatch between meta and shards",
            field="row_count",
        )
    return {"meta": meta, "rows": rows, "n_shards": n_shards}


def read_ledger(path):
    """Dispatch on the version header; v2 is a directory, v1 a JSON file."""
    if os.path.isdir(path):
        return "v2", read_v2(path)
    doc = _load_json(path)
    version = doc.get("version") if isinstance(doc, dict) else None
    if version == "v1":
        return "v1", read_v1(path)
    if version == "v2":
        # single shard file handed in directly
        if doc.get("format") == SHARD_TAG:
            for i, row in enumerate(doc.get("rows", [])):
                _validate_row(row, V2_ROW_FIELDS, "%s#row[%d]" % (path, i))
            return "v2", doc
        raise BusinessError(
            E_FORMAT_CORRUPT, "bad v2 ledger header in %s" % path, field="format"
        )
    raise BusinessError(
        E_VERSION_UNSUPPORTED,
        "unsupported ledger version %r" % (version,),
        field="version",
    )
