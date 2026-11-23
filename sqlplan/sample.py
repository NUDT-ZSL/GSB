from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .models import PlanRecord, ViewDef
from .planner import PlanBuilder
from .util import atomic_write_bytes, atomic_write_json, digest_object, sha256_bytes


INITIAL_CLOCKS = {
    "users": 1_700_000_000_000,
    "orders": 1_700_000_001_000,
    "paid_orders": 1_700_000_002_000,
    "user_totals": 1_700_000_003_000,
    "daily_totals": 1_700_000_004_000,
}

INITIAL_SQL = {
    "users": "SELECT id, name, country FROM raw_users",
    "orders": ("SELECT id, user_id, amount, status, created_at FROM raw_orders"),
    "paid_orders": (
        "SELECT o.id AS order_id, o.user_id, u.name AS user_name, o.amount "
        "FROM raw_orders o JOIN users u ON o.user_id = u.id "
        "WHERE o.status = 'paid'"
    ),
    "user_totals": (
        "SELECT p.user_id, p.user_name, COUNT(*) AS order_count, "
        "SUM(p.amount) AS total_amount FROM paid_orders p "
        "GROUP BY p.user_id, p.user_name"
    ),
    "daily_totals": (
        "SELECT substr(p.order_id, 1, 8) AS day_key, "
        "SUM(p.amount) AS total_amount FROM paid_orders p "
        "GROUP BY substr(p.order_id, 1, 8)"
    ),
}

UPSTREAM = {
    "users": [],
    "orders": [],
    "paid_orders": ["users", "orders"],
    "user_totals": ["paid_orders"],
    "daily_totals": ["paid_orders"],
}


def make_view(
    name: str,
    sql: str,
    upstream: Sequence[str],
    logical_clock_ms: int,
) -> ViewDef:
    payload = {
        "name": name,
        "sql": sql,
        "upstream": [str(item) for item in upstream],
        "logical_clock_ms": logical_clock_ms,
    }
    return ViewDef(
        name=name,
        sql=sql,
        upstream=tuple(str(item) for item in upstream),
        logical_clock_ms=logical_clock_ms,
        content_digest="sha256:" + digest_object(payload),
    )


def initial_views() -> Dict[str, ViewDef]:
    return {
        name: make_view(
            name,
            INITIAL_SQL[name],
            UPSTREAM[name],
            INITIAL_CLOCKS[name],
        )
        for name in INITIAL_SQL
    }


def _view_payload(view: ViewDef) -> Dict[str, Any]:
    return view.to_dict()


def _write_views(root: Path, views: Mapping[str, ViewDef]) -> None:
    atomic_write_json(
        root / "views.json",
        {
            "format": "sqlplan/views/v1",
            "logical_clock_ms": max(view.logical_clock_ms for view in views.values()),
            "views": [_view_payload(views[name]) for name in sorted(views)],
        },
    )


def _write_v1(
    root: Path,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
) -> None:
    column_map: Dict[str, Any] = {}
    plans: Dict[str, Any] = {}
    for name in sorted(views):
        record = records[name]
        plans[name] = {
            "plan_digest": record.plan_digest,
            "logical_clock_ms": views[name].logical_clock_ms,
        }
        for column in record.columns:
            key = f"{name}.{column['name']}"
            column_map[key] = {
                "view": name,
                "column": column["name"],
                "ordinal": column["ordinal"],
                "expression": column["expression"],
                "expression_digest": column["expression_digest"],
            }
    atomic_write_json(
        root / "snapshots" / "v1.json",
        {
            "format": "sqlplan/snapshot/v1",
            "snapshot_id": "v1-flat-2024",
            "logical_clock_ms": 1_700_000_005_000,
            "plan_generation": 2,
            "generated_at_relative_seconds": 4.25,
            "plans": plans,
            "column_map": column_map,
        },
    )


def _shard_payload(
    name: str,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
) -> Dict[str, Any]:
    entries = []
    for view_name in sorted(name for name in views if name in records):
        record = records[view_name]
        entries.append(
            {
                "name": view_name,
                "logical_clock_ms": views[view_name].logical_clock_ms,
                "plan_generation": 3,
                "plan_digest": record.plan_digest,
                "columns": [
                    {
                        "name": column["name"],
                        "ordinal": column["ordinal"],
                        "expression": column["expression"],
                        "expression_digest": column["expression_digest"],
                    }
                    for column in record.columns
                ],
                "executable_sql": record.executable_sql,
                "tombstone": False,
            }
        )
    return {
        "format": "sqlplan/snapshot/v2-shard",
        "shard_id": name,
        "plan_generation": 3,
        "logical_clock_ms": 1_700_000_009_000,
        "generated_at_ms": 1_700_000_009_500,
        "views": entries,
    }


def _write_v2_meta(
    meta_path: Path,
    shard_rows: Sequence[Mapping[str, Any]],
    tombstones: Sequence[Mapping[str, Any]],
) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    if meta_path.exists():
        meta_path.unlink()
    connection = sqlite3.connect(meta_path)
    try:
        connection.execute(
            """
            CREATE TABLE shard_manifest (
                seq INTEGER PRIMARY KEY,
                shard_file TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                plan_generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                view_count INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE tombstones (
                view_name TEXT NOT NULL,
                plan_generation INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                reason TEXT,
                PRIMARY KEY(view_name, plan_generation)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO shard_manifest(
                seq, shard_file, sha256, plan_generation,
                logical_clock_ms, generated_at_ms, view_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    index + 1,
                    row["shard_file"],
                    row["sha256"],
                    row["plan_generation"],
                    row["logical_clock_ms"],
                    row["generated_at_ms"],
                    row["view_count"],
                )
                for index, row in enumerate(shard_rows)
            ],
        )
        connection.executemany(
            """
            INSERT INTO tombstones(
                view_name, plan_generation, logical_clock_ms,
                generated_at_ms, reason
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    row["view_name"],
                    row["plan_generation"],
                    row["logical_clock_ms"],
                    row["generated_at_ms"],
                    row.get("reason"),
                )
                for row in tombstones
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _write_v2(
    root: Path,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
) -> None:
    v2_dir = root / "snapshots" / "v2"
    if v2_dir.exists():
        for path in sorted(v2_dir.glob("*")):
            if path.is_file():
                path.unlink()
    v2_dir.mkdir(parents=True, exist_ok=True)
    groups = {
        "shard-01-dimensions.json": ["users", "orders"],
        "shard-02-metrics.json": [
            "paid_orders",
            "user_totals",
            "daily_totals",
        ],
    }
    shard_rows = []
    for file_name, names in groups.items():
        payload = _shard_payload(
            file_name, {name: views[name] for name in names}, records
        )
        data = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        path = v2_dir / file_name
        atomic_write_bytes(path, data)
        shard_rows.append(
            {
                "shard_file": file_name,
                "sha256": sha256_bytes(data),
                "plan_generation": 3,
                "logical_clock_ms": 1_700_000_009_000,
                "generated_at_ms": 1_700_000_009_500,
                "view_count": len(payload["views"]),
            }
        )
    _write_v2_meta(
        v2_dir / "v2_meta.db",
        shard_rows,
        [
            {
                "view_name": "legacy_alias",
                "plan_generation": 3,
                "logical_clock_ms": 1_700_000_008_500,
                "generated_at_ms": 1_700_000_009_000,
                "reason": "renamed_to_user_totals",
            }
        ],
    )


def _patch_view(
    views: Mapping[str, ViewDef],
    name: str,
    sql: str,
    upstream: Sequence[str],
    clock: int,
) -> Dict[str, Any]:
    view = make_view(name, sql, upstream, clock)
    return {
        "op": "modify",
        "name": name,
        "sql": sql,
        "upstream": list(upstream),
        "logical_clock_ms": clock,
        "content_digest": view.content_digest,
    }


def _add_view(
    name: str,
    sql: str,
    upstream: Sequence[str],
    clock: int,
) -> Dict[str, Any]:
    view = make_view(name, sql, upstream, clock)
    return {
        "op": "add",
        "view": view.to_dict(),
    }


def _write_patches(root: Path, views: Mapping[str, ViewDef]) -> None:
    patches_dir = root / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    patches = {
        "01_modify_paid_amount.json": {
            "patch_id": "p-paid-amount-v2",
            "edit_round": 1,
            "logical_clock_ms": 1_700_000_020_000,
            "operations": [
                _patch_view(
                    views,
                    "paid_orders",
                    (
                        "SELECT o.id AS order_id, o.user_id, u.name AS user_name, "
                        "o.amount * 1.05 AS amount "
                        "FROM raw_orders o JOIN users u ON o.user_id = u.id "
                        "WHERE o.status = 'paid'"
                    ),
                    ["users", "orders"],
                    1_700_000_020_000,
                )
            ],
        },
        "02_add_users_tier.json": {
            "patch_id": "p-users-tier",
            "edit_round": 2,
            "logical_clock_ms": 1_700_000_021_000,
            "operations": [
                _patch_view(
                    views,
                    "users",
                    (
                        "SELECT id, name, country, "
                        "CASE WHEN country = 'CN' THEN 'domestic' "
                        "ELSE 'international' END AS tier "
                        "FROM raw_users"
                    ),
                    [],
                    1_700_000_021_000,
                )
            ],
        },
        "03_rename_referenced_id_invalid.json": {
            "patch_id": "p-users-rename-id-invalid",
            "edit_round": 3,
            "logical_clock_ms": 1_700_000_022_000,
            "operations": [
                _patch_view(
                    views,
                    "users",
                    "SELECT id AS user_key, name, country FROM raw_users",
                    [],
                    1_700_000_022_000,
                )
            ],
        },
        "04_add_paid_by_day.json": {
            "patch_id": "p-add-paid-by-day",
            "edit_round": 4,
            "logical_clock_ms": 1_700_000_023_000,
            "operations": [
                _add_view(
                    "paid_by_day",
                    (
                        "SELECT p.order_id, p.amount, u.country "
                        "FROM paid_orders p JOIN users u ON p.user_id = u.id"
                    ),
                    ["paid_orders", "users"],
                    1_700_000_023_000,
                )
            ],
        },
        "05_delete_paid_by_day.json": {
            "patch_id": "p-delete-paid-by-day",
            "edit_round": 5,
            "logical_clock_ms": 1_700_000_024_000,
            "operations": [{"op": "delete", "name": "paid_by_day"}],
        },
    }
    for file_name, payload in patches.items():
        atomic_write_json(patches_dir / file_name, payload)


def create_sample(root: str | Path = ".") -> Dict[str, Any]:
    root = Path(root)
    views = initial_views()
    records = PlanBuilder(views).build_all()
    _write_views(root, views)
    _write_v1(root, views, records)
    _write_v2(root, views, records)
    _write_patches(root, views)
    return {
        "root": str(root.resolve()),
        "views": sorted(views),
        "plan_digests": {name: records[name].plan_digest for name in sorted(records)},
        "v1": str((root / "snapshots" / "v1.json").resolve()),
        "v2": str((root / "snapshots" / "v2").resolve()),
        "patches": sorted(path.name for path in (root / "patches").glob("*.json")),
    }


if __name__ == "__main__":
    print(json.dumps(create_sample(Path.cwd()), ensure_ascii=False, indent=2))
