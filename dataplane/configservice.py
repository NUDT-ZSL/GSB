from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .config import classify_config_change, merge_config, read_v2_package, validate_v1_config, read_json
from .db import advance_clock, get_meta, immediate, set_meta
from .errors import ErrorCode
from .runtime import maybe_crash


def _active_row(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    version = get_meta(conn, "active_config_version")
    if not version:
        row = conn.execute(
            "SELECT * FROM config_snapshot ORDER BY config_version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM config_snapshot WHERE config_version=?", (int(version),)
        ).fetchone()
    return dict(row) if row else None


def active_config(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    return _active_row(conn)


def install_config_package(
    conn: sqlite3.Connection,
    package_dir: Path | str,
    data_dir: Path | str,
    clock_ms: int,
    tolerance_ms: int = 5_000,
    *,
    record_pending: bool = True,
) -> Dict[str, Any]:
    package_dir = Path(package_dir).resolve()
    data_dir = Path(data_dir).resolve()
    watermark = advance_clock(conn, clock_ms, tolerance_ms)
    package = read_v2_package(package_dir)

    active = _active_row(conn)
    classification = classify_config_change(active, package)
    if classification == "noop":
        with immediate(conn):
            set_meta(conn, "pending_config", "")
        return {
            "classification": "noop",
            "config_version": package["manifest_version"],
            "package_id": package["package_id"],
            "package_hash": package["package_hash"],
            "clock_ms": watermark,
        }

    v1 = _resolve_v1(conn, active, data_dir)
    merged = merge_config(v1, package)
    if record_pending:
        with immediate(conn):
            set_meta(
                conn,
                "pending_config",
                json.dumps(
                    {
                        "package_dir": str(package_dir),
                        "requested_clock_ms": clock_ms,
                        "tolerance_ms": tolerance_ms,
                    },
                    sort_keys=True,
                ),
            )

    with immediate(conn):
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _config_stage_shard ("
            "path TEXT PRIMARY KEY, sha256 TEXT, generation INTEGER, shard_json TEXT)"
        )
        conn.execute("DELETE FROM _config_stage_shard")
        shard_rows = []
        for ref in package["shards"]:
            shard_path = package_dir / ref["path"]
            shard = read_json(shard_path)
            shard_rows.append(
                (
                    ref["path"],
                    ref["sha256"],
                    ref["generation"],
                    json.dumps(shard, sort_keys=True, separators=(",", ":")),
                )
            )
        conn.executemany(
            "INSERT INTO _config_stage_shard(path, sha256, generation, shard_json) "
            "VALUES (?, ?, ?, ?)",
            shard_rows,
        )
        staged_count = conn.execute(
            "SELECT COUNT(*) AS n FROM _config_stage_shard"
        ).fetchone()["n"]
        if staged_count != len(package["shards"]):
            raise RuntimeError("config staging row count mismatch")

        maybe_crash("config_staged", str(package["manifest_version"]))

        manifest_for_storage = {
            "v1": v1,
            "package_manifest": package["manifest"],
            "shards": package["shards"],
            "key_set": package["key_set"],
            "tombstones": package["tombstones"],
        }
        conn.execute(
            "INSERT INTO config_snapshot("
            "config_version, package_id, package_hash, format_generation, "
            "policy_generation_floor, effective_at_ms, classification, manifest_json, "
            "merged_json, merged_digest, key_set_digest, activated_clock_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(package_hash) DO NOTHING",
            (
                package["manifest_version"],
                package["package_id"],
                package["package_hash"],
                "v1+v2",
                package["policy_generation_floor"],
                package["effective_at_ms"],
                classification,
                json.dumps(manifest_for_storage, sort_keys=True, separators=(",", ":")),
                json.dumps(merged, sort_keys=True, separators=(",", ":")),
                merged["merged_digest"],
                package["cache_key_set_digest"],
                watermark,
            ),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO config_shard("
            "config_version, path, sha256, generation, shard_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    package["manifest_version"],
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                )
                for row in shard_rows
            ],
        )
        set_meta(conn, "active_config_version", str(package["manifest_version"]))
        set_meta(conn, "active_config_digest", merged["merged_digest"])
        set_meta(conn, "pending_config", "")
        maybe_crash("config_before_commit", str(package["manifest_version"]))

    maybe_crash("config_after_commit", str(package["manifest_version"]))
    return {
        "classification": classification,
        "config_version": package["manifest_version"],
        "policy_generation": merged["policy_generation"],
        "package_id": package["package_id"],
        "package_hash": package["package_hash"],
        "merged_digest": merged["merged_digest"],
        "clock_ms": watermark,
    }


def install_initial_config(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    clock_ms: int,
    tolerance_ms: int = 5_000,
) -> Dict[str, Any]:
    data_dir = Path(data_dir)
    v1 = validate_v1_config(read_json(data_dir / "config_v1.json"))
    return install_config_package(
        conn,
        data_dir / "config_v2",
        data_dir,
        clock_ms,
        tolerance_ms,
        record_pending=False,
    )


def _resolve_v1(
    conn: sqlite3.Connection, active: Optional[Dict[str, Any]], data_dir: Path
) -> Dict[str, Any]:
    if active and active.get("manifest_json"):
        manifest = json.loads(active["manifest_json"])
        if isinstance(manifest.get("v1"), dict):
            return manifest["v1"]
    return validate_v1_config(
        read_json(
            data_dir / "config_v1.json",
            malformed_code=ErrorCode.CONFIG_V1_INVALID,
        )
    )


def recover_pending_config(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    clock_ms: Optional[int] = None,
    tolerance_ms: int = 5_000,
) -> Optional[Dict[str, Any]]:
    raw = get_meta(conn, "pending_config", "")
    if not raw:
        return None
    pending = json.loads(raw)
    effective_clock = clock_ms
    if effective_clock is None:
        effective_clock = int(get_meta(conn, "clock_watermark_ms", "0") or 0)
    return install_config_package(
        conn,
        pending["package_dir"],
        data_dir,
        int(effective_clock),
        int(pending.get("tolerance_ms", tolerance_ms)),
        record_pending=False,
    )
