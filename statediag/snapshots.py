"""两代配置快照的读取、迁移与整体替换。

v1：单文件 JSON，记录相对生效秒与扁平参数，无代数/墓碑。
v2：分片 JSON + SQLite 元数据库，记录绝对毫秒、配置代数与失效墓碑。
"""

import json
import os
import sqlite3

from .errors import ESnapshotRollback, EValidation

SNAPSHOT_V1 = 1
SNAPSHOT_V2 = 2


def _sha256_file(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _canonical(payload):
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)


def write_v1(directory, config, origin_ms=0):
    """写 v1 单文件快照：相对生效秒 + 扁平参数。"""
    os.makedirs(directory, exist_ok=True)
    payload = {
        "version": SNAPSHOT_V1,
        "effective_seconds": (config.effective_ms - origin_ms) / 1000.0,
        "parameters": {
            "algebra": config.algebra,
            "precision": config.precision,
            "rounding_mode": config.rounding_mode,
            "tolerance_abs": float(config.tolerance_abs),
            "tolerance_rel": float(config.tolerance_rel),
        },
    }
    path = os.path.join(directory, "config_v1.json")
    _atomic_write(path, _canonical(payload))
    return path


def read_v1(path, origin_ms=0):
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("version") != SNAPSHOT_V1:
        raise EValidation("不是 v1 快照", details={"path": path})
    params = payload["parameters"]
    return {
        "version": SNAPSHOT_V1,
        "effective_ms": origin_ms + int(round(payload["effective_seconds"] * 1000)),
        "algebra": params["algebra"],
        "precision": params["precision"],
        "rounding_mode": params["rounding_mode"],
        "tolerance_abs": float(params["tolerance_abs"]),
        "tolerance_rel": float(params["tolerance_rel"]),
        "tombstones": [],
        "checksum": _sha256_file(path),
    }


def write_v2(directory, config, tombstones=None):
    """写 v2：分片 JSON（参数/墓碑各一片）+ SQLite 元数据库。"""
    os.makedirs(directory, exist_ok=True)
    tombstones = list(tombstones or [])
    param_shard = {
        "version": SNAPSHOT_V2,
        "effective_ms": int(config.effective_ms),
        "algebra": config.algebra,
        "precision": config.precision,
        "rounding_mode": config.rounding_mode,
        "tolerance_abs": float(config.tolerance_abs),
        "tolerance_rel": float(config.tolerance_rel),
    }
    tomb_shard = {"version": SNAPSHOT_V2, "tombstones": tombstones}
    param_path = os.path.join(directory, "config_v2.parameters.json")
    tomb_path = os.path.join(directory, "config_v2.tombstones.json")
    _atomic_write(param_path, _canonical(param_shard))
    _atomic_write(tomb_path, _canonical(tomb_shard))

    db_path = os.path.join(directory, "config_v2.meta.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        meta_rows = [
            ("version", str(SNAPSHOT_V2)),
            ("effective_ms", str(int(config.effective_ms))),
            ("algebra", str(config.algebra)),
            ("parameters_checksum", _sha256_file(param_path)),
            ("tombstones_checksum", _sha256_file(tomb_path)),
        ]
        cur.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta_rows)
        cur.execute(
            "CREATE TABLE tombstones (tombstone_id TEXT PRIMARY KEY, "
            "algebra INTEGER NOT NULL, reason TEXT NOT NULL)"
        )
        tomb_rows = [
            (t["tombstone_id"], int(t["algebra"]), t["reason"])
            for t in sorted(tombstones, key=lambda x: x["tombstone_id"])
        ]
        cur.executemany(
            "INSERT INTO tombstones (tombstone_id, algebra, reason) "
            "VALUES (?, ?, ?)",
            tomb_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "parameters": param_path,
        "tombstones": tomb_path,
        "meta_db": db_path,
    }


def read_v2(directory):
    param_path = os.path.join(directory, "config_v2.parameters.json")
    tomb_path = os.path.join(directory, "config_v2.tombstones.json")
    db_path = os.path.join(directory, "config_v2.meta.sqlite")
    with open(param_path, "r", encoding="utf-8") as handle:
        params = json.load(handle)
    if params.get("version") != SNAPSHOT_V2:
        raise EValidation("不是 v2 快照", details={"path": param_path})
    with open(tomb_path, "r", encoding="utf-8") as handle:
        tomb_payload = json.load(handle)
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, value FROM meta ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
    return {
        "version": SNAPSHOT_V2,
        "effective_ms": int(params["effective_ms"]),
        "algebra": int(params["algebra"]),
        "precision": params["precision"],
        "rounding_mode": params["rounding_mode"],
        "tolerance_abs": float(params["tolerance_abs"]),
        "tolerance_rel": float(params["tolerance_rel"]),
        "tombstones": sorted(
            tomb_payload["tombstones"], key=lambda x: x["tombstone_id"]
        ),
        "checksum": _sha256_file(param_path),
        "db_meta": dict(rows),
    }


def migrate_v1_to_v2(directory, config_v1_path, origin_ms=0, tombstones=None):
    """v1 -> v2：补全绝对毫秒、配置代数与失效墓碑，逐项记录补全来源。"""
    v1 = read_v1(config_v1_path, origin_ms=origin_ms)
    tombstones = list(tombstones or [])
    provenance = [
        {"field": "effective_ms", "source": "derived_from_effective_seconds"},
        {"field": "algebra", "source": "default_to_1_when_absent"},
        {"field": "tombstones", "source": "initialized_empty_plus_explicit"},
    ]
    from .model import TransformConfig

    config = TransformConfig(
        algebra=v1["algebra"],
        precision=v1["precision"],
        rounding_mode=v1["rounding_mode"],
        tolerance_abs=v1["tolerance_abs"],
        tolerance_rel=v1["tolerance_rel"],
        effective_ms=v1["effective_ms"],
    )
    paths = write_v2(directory, config, tombstones=tombstones)
    paths["provenance"] = provenance
    return paths, provenance


def migrate_v2_to_v1(directory, v2_directory, origin_ms=0):
    """v2 -> v1：无法表达的墓碑按显式丢弃规则处理并记录。"""
    v2 = read_v2(v2_directory)
    dropped = [
        {
            "tombstone_id": t["tombstone_id"],
            "rule": "v1_cannot_represent_tombstone_explicitly_dropped",
        }
        for t in v2["tombstones"]
    ]
    from .model import TransformConfig

    config = TransformConfig(
        algebra=v2["algebra"],
        precision=v2["precision"],
        rounding_mode=v2["rounding_mode"],
        tolerance_abs=v2["tolerance_abs"],
        tolerance_rel=v2["tolerance_rel"],
        effective_ms=v2["effective_ms"],
    )
    path = write_v1(directory, config, origin_ms=origin_ms)
    return path, dropped


def guard_no_rollback(current, candidate):
    """版本号或校验和变化只允许前进；回退尝试返回 E_SNAPSHOT_ROLLBACK。"""
    if candidate["version"] < current["version"]:
        raise ESnapshotRollback(
            "配置快照版本号回退被拒绝",
            details={
                "current_version": current["version"],
                "candidate_version": candidate["version"],
            },
        )
    if (
        candidate["version"] == current["version"]
        and candidate.get("checksum") == current.get("checksum")
    ):
        return False
    if candidate["version"] == current["version"] and candidate.get(
        "checksum"
    ) != current.get("checksum"):
        # 同版本内容变化视为整体替换（校验和变化必须整体替换），允许前进。
        return True
    return True
