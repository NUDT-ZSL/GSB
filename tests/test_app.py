from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from sqlplan.errors import BusinessError, ErrorCode
from sqlplan.planner import PlanBuilder
from sqlplan.sample import (
    _shard_payload,
    _write_v1,
    _write_v2_meta,
    _write_views,
    initial_views,
)
from sqlplan.service import AppService
from sqlplan.util import atomic_write_bytes, canonical_json, digest_object
from sqlplan.validation import load_views


ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "views.json"
V1 = ROOT / "snapshots" / "v1.json"
V2 = ROOT / "snapshots" / "v2"
PATCH1 = ROOT / "patches" / "01_modify_paid_amount.json"
PATCH2 = ROOT / "patches" / "02_add_users_tier.json"
PATCH3_INVALID = ROOT / "patches" / "03_rename_referenced_id_invalid.json"
PATCH4_ADD = ROOT / "patches" / "04_add_paid_by_day.json"
PATCH5_DELETE = ROOT / "patches" / "05_delete_paid_by_day.json"


def make_service(root: Path) -> AppService:
    return AppService(
        db_path=root / "state.db",
        lock_path=root / "app.lock",
        log_dir=root / "logs",
        lock_timeout_seconds=10,
    )


def load(service: AppService, **overrides: Any) -> Dict[str, Any]:
    values = {
        "views_path": VIEWS,
        "v1_path": V1,
        "v2_dir": V2,
        "v2_meta_path": None,
        "injected_clock_ms": None,
    }
    values.update(overrides)
    return service.load(**values)


def view_json(
    name: str,
    sql: str,
    upstream: Sequence[str],
    clock: int = 1_700_000_000_000,
) -> Dict[str, Any]:
    payload = {
        "name": name,
        "sql": sql,
        "upstream": list(upstream),
        "logical_clock_ms": clock,
    }
    return {**payload, "content_digest": "sha256:" + digest_object(payload)}


def write_views(path: Path, views: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "sqlplan/views/v1",
                "views": list(views),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def run_cli(
    db: Path,
    lock: Path,
    logs: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "sqlplan",
        "--db",
        str(db),
        "--lock",
        str(lock),
        "--logs",
        str(logs),
        *args,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, **dict(env or {})},
        timeout=30,
    )


class PlanTests(unittest.TestCase):
    def test_planner_is_deterministic_and_contains_required_nodes(self) -> None:
        views, _ = load_views(VIEWS)
        first = PlanBuilder(views).build_all()
        second = PlanBuilder(views).build_all()
        self.assertEqual(
            {name: record.plan_digest for name, record in first.items()},
            {name: record.plan_digest for name, record in second.items()},
        )
        for name in first:
            self.assertEqual(
                canonical_json(first[name].plan),
                canonical_json(second[name].plan),
            )
            self.assertEqual(first[name].executable_sql, second[name].executable_sql)
            self.assertEqual(
                first[name].plan_digest,
                "sha256:" + digest_object(first[name].plan),
            )
        kinds = {
            node["kind"] for record in first.values() for node in record.plan["nodes"]
        }
        self.assertTrue({"scan", "filter", "project", "aggregate", "join"} <= kinds)
        paid = first["paid_orders"].plan
        self.assertTrue(any(node["join_keys"] for node in paid["nodes"]))
        self.assertTrue(any(node["predicate_columns"] for node in paid["nodes"]))
        for column in paid["output_columns"]:
            self.assertIn("expression_digest", column)

    def test_group_having_and_order_by_can_reference_projection_alias(self) -> None:
        from sqlplan.models import ViewDef

        sql = (
            "SELECT user_id, SUM(amount) AS total FROM orders "
            "GROUP BY user_id HAVING total > 1 ORDER BY total DESC"
        )
        payload = {
            "name": "alias_usage",
            "sql": sql,
            "upstream": [],
            "logical_clock_ms": 1,
        }
        view = ViewDef(
            "alias_usage",
            sql,
            (),
            1,
            "sha256:" + digest_object(payload),
        )
        record = PlanBuilder({"alias_usage": view}).build_all()["alias_usage"]
        contexts = {
            ref["context"]
            for node in record.plan["nodes"]
            for ref in node["predicate_columns"]
        }
        self.assertIn("having", contexts)
        self.assertIn("order_by", contexts)


class LoadAndMergeTests(unittest.TestCase):
    def test_load_merges_v1_v2_and_status_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            result = load(service)
            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"], "initial_load")
            self.assertEqual(result["plan_generation"], 3)
            merged = result["snapshot_merge"]
            self.assertEqual(merged["v1_time_conversion"]["scale_ms_per_second"], 1000)
            self.assertEqual(merged["v1_time_conversion"]["rounding"], "half_up")
            self.assertEqual(merged["v1_time_conversion"]["delta_ms"], 4250)
            self.assertEqual(merged["views"]["users"]["winner"]["source_version"], "v2")
            self.assertTrue(merged["views"]["legacy_alias"]["tombstone"])
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 3)
            self.assertEqual(status["pending_invalidation_events"], 0)
            self.assertEqual(
                [item["view"] for item in status["views"]],
                sorted(item["view"] for item in status["views"]),
            )
            status_json = json.dumps(status, sort_keys=True, ensure_ascii=False)
            self.assertEqual(
                status_json,
                json.dumps(service.status(), sort_keys=True, ensure_ascii=False),
            )

    def test_second_identical_load_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            second = load(service)
            self.assertEqual(second["decision"], "noop")
            with sqlite3.connect(service.db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM plan_generations"
                    ).fetchone()[0],
                    1,
                )

    def test_invalid_sql_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_views = root / "views.json"
            write_views(
                bad_views,
                [view_json("broken", "SELECT FROM", [])],
            )
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, views_path=bad_views)
            self.assertEqual(raised.exception.code, ErrorCode.E_SQL_PARSE)
            self.assertFalse(service.db_path.exists())

    def test_missing_json_file_returns_business_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, views_path=root / "missing.json")
            self.assertEqual(raised.exception.code, ErrorCode.E_BAD_JSON)
            self.assertFalse(service.db_path.exists())

    def test_cycle_returns_e_cycle_without_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_views = root / "views.json"
            write_views(
                bad_views,
                [
                    view_json("a", "SELECT id FROM b", ["b"]),
                    view_json("b", "SELECT id FROM a", ["a"]),
                ],
            )
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, views_path=bad_views)
            self.assertEqual(raised.exception.code, ErrorCode.E_CYCLE)
            self.assertIn("cycle", raised.exception.details)
            self.assertFalse(service.db_path.exists())

    def test_shard_hash_mismatch_is_distinct_and_write_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v2_copy = root / "v2"
            shutil.copytree(V2, v2_copy)
            shard = sorted(v2_copy.glob("*.json"))[0]
            shard.write_text(shard.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, v2_dir=v2_copy)
            self.assertEqual(raised.exception.code, ErrorCode.E_SHARD_HASH_MISMATCH)
            self.assertFalse(service.db_path.exists())

    def test_v1_untraceable_column_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v1_data = json.loads(V1.read_text(encoding="utf-8"))
            v1_data["column_map"]["users.not_a_column"] = {
                "view": "users",
                "column": "not_a_column",
            }
            v1_copy = root / "v1.json"
            v1_copy.write_text(json.dumps(v1_data), encoding="utf-8")
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, v1_path=v1_copy)
            self.assertEqual(raised.exception.code, ErrorCode.E_BAD_SNAPSHOT)
            self.assertIn("not_a_column", str(raised.exception.details))
            self.assertFalse(service.db_path.exists())

    def test_v2_generation_monotonicity_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v2_copy = root / "v2"
            shutil.copytree(V2, v2_copy)
            with sqlite3.connect(v2_copy / "v2_meta.db") as connection:
                connection.execute(
                    "UPDATE shard_manifest SET plan_generation=4 WHERE seq=1"
                )
                connection.execute(
                    "UPDATE shard_manifest SET plan_generation=3 WHERE seq=2"
                )
            service = make_service(root)
            with self.assertRaises(BusinessError) as raised:
                load(service, v2_dir=v2_copy)
            self.assertEqual(raised.exception.code, ErrorCode.E_BAD_SNAPSHOT)
            self.assertIn("单调", raised.exception.message)
            self.assertFalse(service.db_path.exists())

    def test_stale_snapshot_digest_is_recomputed_not_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v2_copy = root / "v2-stale"
            shutil.copytree(V2, v2_copy)
            target_shard = None
            for shard in sorted(v2_copy.glob("*.json")):
                payload = json.loads(shard.read_text(encoding="utf-8"))
                changed = False
                for entry in payload["views"]:
                    if entry["name"] == "daily_totals":
                        entry["plan_digest"] = "sha256:" + "0" * 64
                        changed = True
                if not changed:
                    continue
                target_shard = shard
                data = (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n"
                ).encode("utf-8")
                shard.write_bytes(data)
                digest = hashlib.sha256(data).hexdigest()
            self.assertIsNotNone(target_shard)
            with sqlite3.connect(v2_copy / "v2_meta.db") as connection:
                connection.execute(
                    "UPDATE shard_manifest SET sha256=? WHERE shard_file=?",
                    (digest, target_shard.name),
                )
            service = make_service(root)
            result = load(service, v2_dir=v2_copy)
            self.assertTrue(result["ok"])
            self.assertEqual(result["snapshot_recompute_required"], ["daily_totals"])
            self.assertEqual(
                result["snapshot_merge"]["views"]["daily_totals"][
                    "snapshot_cache_verdict"
                ],
                "recompute_stale",
            )
            self.assertIn(
                "daily_totals",
                [view["view"] for view in service.status()["views"]],
            )
            self.assertTrue(service.verify()["ok"])

    def test_declared_view_tombstone_is_removed_from_unified_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            v2_copy = root / "v2-tombstone"
            shutil.copytree(V2, v2_copy)
            with sqlite3.connect(v2_copy / "v2_meta.db") as connection:
                connection.execute(
                    "INSERT INTO tombstones("
                    "view_name, plan_generation, logical_clock_ms, "
                    "generated_at_ms, reason) VALUES (?, ?, ?, ?, ?)",
                    (
                        "daily_totals",
                        4,
                        1_700_000_015_000,
                        1_700_000_015_500,
                        "retired_metric",
                    ),
                )
            service = make_service(root)
            result = load(service, v2_dir=v2_copy)
            self.assertEqual(result["plan_generation"], 4)
            self.assertTrue(
                result["snapshot_merge"]["views"]["daily_totals"]["tombstone"]
            )
            self.assertNotIn(
                "daily_totals",
                [view["view"] for view in service.status()["views"]],
            )
            self.assertTrue(service.verify()["ok"])

    def test_same_generation_different_package_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            v1_data = json.loads(V1.read_text(encoding="utf-8"))
            v1_data["generated_at_relative_seconds"] = 4.5
            v1_copy = root / "v1-changed.json"
            v1_copy.write_text(json.dumps(v1_data), encoding="utf-8")
            with self.assertRaises(BusinessError) as raised:
                load(service, v1_path=v1_copy)
            self.assertEqual(raised.exception.code, ErrorCode.E_GENERATION_CONFLICT)
            self.assertEqual(service.status()["current_plan_generation"], 3)

    def test_version_rollback_is_rejected_after_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            service.edit(PATCH1)
            with self.assertRaises(BusinessError) as raised:
                load(service)
            self.assertEqual(raised.exception.code, ErrorCode.E_GENERATION_ROLLBACK)
            self.assertEqual(service.status()["current_plan_generation"], 4)

    def test_higher_generation_same_views_is_incremental_append(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            v2_copy = root / "v2-upgrade"
            shutil.copytree(V2, v2_copy)
            rows = []
            for index, shard in enumerate(sorted(v2_copy.glob("*.json")), start=1):
                payload = json.loads(shard.read_text(encoding="utf-8"))
                payload["plan_generation"] = 4
                payload["logical_clock_ms"] = 1_700_000_010_000 + index
                payload["generated_at_ms"] = 1_700_000_011_000 + index
                for entry in payload["views"]:
                    entry["plan_generation"] = 4
                data = (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                ).encode("utf-8")
                shard.write_bytes(data)
                rows.append(
                    {
                        "shard_file": shard.name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "plan_generation": 4,
                        "logical_clock_ms": payload["logical_clock_ms"],
                        "generated_at_ms": payload["generated_at_ms"],
                        "view_count": len(payload["views"]),
                    }
                )
            with sqlite3.connect(v2_copy / "v2_meta.db") as connection:
                for row in rows:
                    connection.execute(
                        "UPDATE shard_manifest SET sha256=?, plan_generation=?, "
                        "logical_clock_ms=?, generated_at_ms=?, view_count=? "
                        "WHERE shard_file=?",
                        (
                            row["sha256"],
                            row["plan_generation"],
                            row["logical_clock_ms"],
                            row["generated_at_ms"],
                            row["view_count"],
                            row["shard_file"],
                        ),
                    )
            result = load(service, v2_dir=v2_copy)
            self.assertEqual(result["decision"], "incremental_append")
            self.assertEqual(result["plan_generation"], 4)
            self.assertEqual(result["invalidation"]["summary"]["event_count"], 0)
            self.assertTrue(service.verify()["ok"])

    def test_higher_generation_changed_view_set_is_complete_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            views = initial_views()
            views.pop("daily_totals")
            records = PlanBuilder(views).build_all()
            _write_views(root, views)
            _write_v1(root, views, records)
            v2_dir = root / "snapshots" / "v2"
            v2_dir.mkdir(parents=True)
            payload = _shard_payload("shard-all.json", views, records)
            payload["plan_generation"] = 4
            payload["logical_clock_ms"] = 1_700_000_011_000
            payload["generated_at_ms"] = 1_700_000_012_000
            for entry in payload["views"]:
                entry["plan_generation"] = 4
            data = (
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
            atomic_write_bytes(v2_dir / "shard-all.json", data)
            _write_v2_meta(
                v2_dir / "v2_meta.db",
                [
                    {
                        "shard_file": "shard-all.json",
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "plan_generation": 4,
                        "logical_clock_ms": 1_700_000_011_000,
                        "generated_at_ms": 1_700_000_012_000,
                        "view_count": len(payload["views"]),
                    }
                ],
                [
                    {
                        "view_name": "daily_totals",
                        "plan_generation": 4,
                        "logical_clock_ms": 1_700_000_011_000,
                        "generated_at_ms": 1_700_000_012_000,
                        "reason": "view_removed",
                    }
                ],
            )
            result = service.load(
                views_path=root / "views.json",
                v1_path=root / "snapshots" / "v1.json",
                v2_dir=v2_dir,
            )
            self.assertEqual(result["decision"], "complete_replace")
            self.assertEqual(result["plan_generation"], 4)
            self.assertEqual(result["invalidation"]["deleted_views"], ["daily_totals"])
            self.assertNotIn(
                "daily_totals", [view["view"] for view in service.status()["views"]]
            )
            self.assertTrue(service.verify()["ok"])


class EditTests(unittest.TestCase):
    def test_edit_closure_paths_and_unchanged_cache_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            with sqlite3.connect(service.db_path) as connection:
                before = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "SELECT view_name, plan_json, plan_digest, columns_json, "
                        "executable_sql, resolution_json "
                        "FROM plans WHERE plan_generation=3 AND view_name IN "
                        "('orders','users')"
                    )
                }
            result = service.edit(PATCH1)
            self.assertEqual(result["plan_generation"], 4)
            self.assertEqual(
                result["recomputed_views"],
                ["daily_totals", "paid_orders", "user_totals"],
            )
            self.assertEqual(result["reused_views"], ["orders", "users"])
            events = result["invalidations"]["events"]
            column_events = [
                event
                for event in events
                if event["kind"] == "column_resolution_invalidated"
            ]
            self.assertEqual(
                {event["view"] for event in column_events},
                {"daily_totals", "user_totals"},
            )
            self.assertTrue(
                all(
                    event["propagation_path"][0] == "paid_orders"
                    for event in column_events
                )
            )
            self.assertTrue(all(event["node_id"] for event in column_events))
            with sqlite3.connect(service.db_path) as connection:
                after = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "SELECT view_name, plan_json, plan_digest, columns_json, "
                        "executable_sql, resolution_json "
                        "FROM plans WHERE plan_generation=4 AND view_name IN "
                        "('orders','users')"
                    )
                }
            self.assertEqual(before, after)

    def test_unused_added_column_keeps_downstream_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            result = service.edit(PATCH2)
            self.assertEqual(result["recomputed_views"], ["users"])
            self.assertEqual(
                result["reused_views"],
                ["daily_totals", "orders", "paid_orders", "user_totals"],
            )
            self.assertEqual(
                result["invalidations"]["column_changes"]["users"]["added"],
                ["tier"],
            )
            self.assertTrue(service.verify()["ok"])

    def test_referenced_rename_is_isolated_and_write_free(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            with sqlite3.connect(service.db_path) as connection:
                before = (
                    connection.execute(
                        "SELECT plan_generation FROM current_pointer"
                    ).fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM edit_runs").fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM invalidation_events"
                    ).fetchone()[0],
                )
            with self.assertRaises(BusinessError) as raised:
                service.edit(PATCH3_INVALID)
            exc = raised.exception
            self.assertEqual(exc.code, ErrorCode.E_COLUMN_NOT_FOUND)
            self.assertEqual(exc.details["view"], "paid_orders")
            self.assertEqual(exc.details["trigger_view"], "users")
            self.assertEqual(exc.details["column"], "id")
            self.assertEqual(exc.details["node_id"], "paid_orders:join:0")
            self.assertIn("line", exc.details["reference_position"])
            self.assertEqual(exc.details["renamed_columns"][0]["from"], "id")
            self.assertEqual(exc.details["renamed_columns"][0]["to"], "user_key")
            with sqlite3.connect(service.db_path) as connection:
                after = (
                    connection.execute(
                        "SELECT plan_generation FROM current_pointer"
                    ).fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM edit_runs").fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM invalidation_events"
                    ).fetchone()[0],
                )
            self.assertEqual(before, after)

    def test_add_delete_and_duplicate_patch_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            added = service.edit(PATCH4_ADD)
            self.assertEqual(added["plan_generation"], 4)
            self.assertIn("paid_by_day", added["recomputed_views"])
            duplicate = service.edit(PATCH4_ADD)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["plan_generation"], 4)
            deleted = service.edit(PATCH5_DELETE)
            self.assertEqual(deleted["plan_generation"], 5)
            self.assertEqual(deleted["deleted_views"], ["paid_by_day"])
            self.assertNotIn(
                "paid_by_day", [view["view"] for view in service.status()["views"]]
            )
            with sqlite3.connect(service.db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM edit_runs WHERE patch_id=?",
                        ("p-add-paid-by-day",),
                    ).fetchone()[0],
                    1,
                )

    def test_mixed_patch_commits_valid_and_isolates_invalid_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            clock = 1_700_000_020_000
            new_orders = view_json(
                "orders",
                (
                    "SELECT id, user_id, amount, status, created_at "
                    "FROM raw_orders WHERE amount >= 0"
                ),
                [],
                clock,
            )
            patch = {
                "patch_id": "mixed-isolation",
                "edit_round": 1,
                "logical_clock_ms": clock,
                "operations": [
                    {"op": "modify", "view": new_orders},
                    {
                        "op": "add",
                        "view": view_json(
                            "users",
                            "SELECT id FROM raw_users",
                            [],
                            clock,
                        ),
                    },
                ],
            }
            result = service.edit(patch)
            self.assertTrue(result["ok"])
            self.assertEqual(result["plan_generation"], 4)
            self.assertEqual(result["accepted_operations"][0]["name"], "orders")
            self.assertEqual(
                result["rejected_operations"][0]["error"]["code"],
                "E_VIEW_EXISTS",
            )
            status = service.status()
            self.assertEqual(
                [view["view"] for view in status["views"]],
                ["daily_totals", "orders", "paid_orders", "user_totals", "users"],
            )

    def test_coordinated_upstream_rename_and_downstream_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = make_service(Path(temp))
            load(service)
            clock = 1_700_000_020_000
            patch = {
                "patch_id": "coordinated-rename",
                "edit_round": 1,
                "logical_clock_ms": clock,
                "operations": [
                    {
                        "op": "modify",
                        "view": view_json(
                            "users",
                            "SELECT id AS user_key, name, country FROM raw_users",
                            [],
                            clock,
                        ),
                    },
                    {
                        "op": "modify",
                        "view": view_json(
                            "paid_orders",
                            (
                                "SELECT o.id AS order_id, o.user_id, "
                                "u.name AS user_name, o.amount "
                                "FROM raw_orders o JOIN users u "
                                "ON o.user_id = u.user_key "
                                "WHERE o.status = 'paid'"
                            ),
                            ["users", "orders"],
                            clock,
                        ),
                    },
                ],
            }
            result = service.edit(patch)
            self.assertEqual(result["plan_generation"], 4)
            self.assertEqual(len(result["accepted_operations"]), 2)
            self.assertEqual(result["rejected_operations"], [])
            plan = service.plan("paid_orders")
            self.assertIn("u.user_key", plan["executable_sql"])
            self.assertTrue(service.verify()["ok"])


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_initial_load_interruption_is_visible_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "load",
                env={"SQLPLAN_FAULT_POINT": "after_prepare"},
            )
            self.assertEqual(crashed.returncode, 91)
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 0)
            self.assertEqual(status["pending_runs"][0]["status"], "prepared")
            resumed = service.resume()
            self.assertEqual(resumed["current_plan_generation"], 3)
            self.assertEqual(resumed["pending_invalidation_events"], 0)
            self.assertTrue(service.verify()["ok"])

    def test_snapshot_stage_interruption_is_finalized_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "load",
                env={"SQLPLAN_FAULT_POINT": "after_stage"},
            )
            self.assertEqual(crashed.returncode, 91)
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 0)
            self.assertEqual(status["pending_runs"][0]["status"], "staged")
            resumed = service.resume()
            self.assertEqual(resumed["current_plan_generation"], 3)
            self.assertEqual(resumed["pending_invalidation_events"], 0)
            self.assertTrue(service.verify()["ok"])

    def test_after_stage_crash_resume_and_no_duplicate_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "edit",
                str(PATCH1),
                env={"SQLPLAN_FAULT_POINT": "after_stage"},
            )
            self.assertEqual(crashed.returncode, 91)
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 3)
            self.assertGreater(status["pending_invalidation_events"], 0)
            self.assertEqual(status["pending_runs"][0]["status"], "staged")
            resumed = service.resume()
            self.assertEqual(resumed["recovered_count"], 1)
            self.assertEqual(resumed["current_plan_generation"], 4)
            self.assertEqual(resumed["pending_invalidation_events"], 0)
            duplicate = service.edit(PATCH1)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["plan_generation"], 4)
            with sqlite3.connect(service.db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM edit_runs WHERE patch_id=?",
                        ("p-paid-amount-v2",),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM invalidation_events WHERE patch_id=?",
                        ("p-paid-amount-v2",),
                    ).fetchone()[0],
                    len(duplicate["invalidations"]["events"]),
                )

    def test_after_commit_exit_is_recognized_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "edit",
                str(PATCH1),
                env={"SQLPLAN_FAULT_POINT": "after_commit"},
            )
            self.assertEqual(crashed.returncode, 91)
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 4)
            self.assertEqual(status["pending_invalidation_events"], 0)
            self.assertEqual(status["pending_runs"], [])
            resumed = service.resume()
            self.assertEqual(resumed["recovered_count"], 0)
            duplicate = service.edit(PATCH1)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["plan_generation"], 4)

    def test_after_prepare_and_during_finalize_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "edit",
                str(PATCH1),
                env={"SQLPLAN_FAULT_POINT": "after_prepare"},
            )
            self.assertEqual(crashed.returncode, 91)
            self.assertEqual(service.status()["pending_runs"][0]["status"], "prepared")
            service.resume()
            self.assertEqual(service.status()["current_plan_generation"], 4)

            crashed = run_cli(
                service.db_path,
                service.lock_path,
                service.log_dir,
                "edit",
                str(PATCH2),
                env={"SQLPLAN_FAULT_POINT": "during_finalize"},
            )
            self.assertEqual(crashed.returncode, 91)
            status = service.status()
            self.assertEqual(status["current_plan_generation"], 4)
            self.assertEqual(status["pending_runs"][0]["status"], "staged")
            service.resume()
            self.assertEqual(service.status()["current_plan_generation"], 5)
            self.assertEqual(service.status()["pending_invalidation_events"], 0)
            self.assertTrue(service.verify()["ok"])

    def test_concurrent_same_patch_commits_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = make_service(root)
            load(service)
            command = [
                sys.executable,
                "-m",
                "sqlplan",
                "--db",
                str(service.db_path),
                "--lock",
                str(service.lock_path),
                "--logs",
                str(service.log_dir),
                "edit",
                str(PATCH1),
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                outputs.append(json.loads(stdout))
            self.assertEqual(
                sorted(bool(item.get("duplicate")) for item in outputs),
                [False, True],
            )
            self.assertEqual({item["plan_generation"] for item in outputs}, {4})
            with sqlite3.connect(service.db_path) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM edit_runs WHERE patch_id=?",
                        ("p-paid-amount-v2",),
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM plan_generations WHERE plan_generation=4"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM invalidation_events "
                        "WHERE plan_generation=4"
                    ).fetchone()[0],
                    len(outputs[0]["invalidations"]["events"]),
                )


if __name__ == "__main__":
    unittest.main()
