from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gsb_pipeline.errors import (  # noqa: E402
    BusinessError,
    E_COLUMN_UNKNOWN,
    E_DEPENDENCY_CYCLE,
    E_DOWNSTREAM_COLUMN_MISSING,
    E_LOGIC_CLOCK_REGRESSION,
    E_V2_META_MISMATCH,
    E_PLAN_GENERATION_REGRESSION,
    E_SHARD_HASH_MISMATCH,
    E_SQL_SYNTAX,
    E_V1_COLUMN_UNKNOWN,
)
from gsb_pipeline.models import (  # noqa: E402
    load_and_merge_sample,
    load_v1_snapshot,
    load_v2_snapshot,
    load_view_definitions,
    build_view_model,
)
from gsb_pipeline.sqlplan import parse_sql_plan  # noqa: E402
from gsb_pipeline.store import (  # noqa: E402
    apply_patch_stream,
    initialize_state_with_definitions,
    verify_state,
)
from gsb_pipeline.utils import canonical_json  # noqa: E402


def generate_sample(destination: Path) -> None:
    subprocess.check_call(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_sample.py"),
            "--out",
            str(destination),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
    )


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    expect_code: int | None = 0,
) -> dict:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-m", "gsb_pipeline", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
    )
    if expect_code is not None:
        assert completed.returncode == expect_code, (
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
    if completed.stdout.strip():
        return json.loads(completed.stdout)
    return {"returncode": completed.returncode}


def read_events(sample_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (sample_dir / "patch_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_row(state_db: Path, generation: int, view: str) -> sqlite3.Row:
    connection = sqlite3.connect(state_db)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM snapshot_views WHERE plan_generation=? AND view_name=?",
            (generation, view),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


class PipelineTests(unittest.TestCase):
    def test_seed_and_plan_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as left_name, tempfile.TemporaryDirectory() as right_name:
            left = Path(left_name)
            right = Path(right_name)
            generate_sample(left)
            generate_sample(right)
            files = [
                "view_definitions.json",
                "v1_snapshot.json",
                "patch_events.jsonl",
                "sample_manifest.json",
                "sqlite_meta.db",
                "v2_shards/shard-0001.json",
                "v2_shards/shard-0002.json",
                "v2_shards/shard-0003.json",
            ]
            for relative in files:
                self.assertEqual(
                    hashlib.sha256((left / relative).read_bytes()).hexdigest(),
                    hashlib.sha256((right / relative).read_bytes()).hexdigest(),
                    relative,
                )

            definitions = load_view_definitions(left / "view_definitions.json")
            schemas = definitions.source_schemas()
            node_types: set[str] = set()
            for name in definitions.order:
                view = definitions.views[name]
                first = parse_sql_plan(
                    name,
                    view.sql,
                    schemas,
                    dependency_names=definitions.view_names,
                )
                second = parse_sql_plan(
                    name,
                    view.sql,
                    schemas,
                    dependency_names=definitions.view_names,
                )
                self.assertEqual(canonical_json(first), canonical_json(second))
                self.assertEqual(first["plan_summary"], second["plan_summary"])
                for node in first["node_index"]:
                    self.assertTrue(node["node_id"].startswith("nod_"))
                    self.assertEqual(len(node["node_id"]), len("nod_") + 64)
                    node_types.add(node["type"])
            self.assertEqual(
                node_types,
                {"scan", "filter", "aggregate", "join", "project"},
            )
            connection = sqlite3.connect(":memory:")
            try:
                for source, columns in definitions.sources.items():
                    column_sql = ", ".join(f'"{column}" TEXT' for column in columns)
                    connection.execute(f'CREATE TABLE "{source}" ({column_sql})')
                for name in definitions.order:
                    connection.execute(
                        f'CREATE VIEW "{name}" AS {definitions.views[name].executable_sql}'
                    )
                for name in definitions.order:
                    connection.execute(f'SELECT * FROM "{name}" LIMIT 0').fetchall()
                connection.commit()
            finally:
                connection.close()

    def test_cross_version_merge_and_time_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            sample = Path(temp_name)
            generate_sample(sample)
            _definitions, _v1, _v2, merged = load_and_merge_sample(sample)
            views = {view["name"]: view for view in merged["views"]}
            tombstones = {item["name"]: item for item in merged["tombstones"]}
            self.assertEqual(merged["counts"]["active_views"], 8)
            self.assertEqual(merged["counts"]["v1_only"], 1)
            self.assertEqual(views["high_value_customers"]["source_version"], "v1")
            self.assertEqual(views["raw_orders"]["source_version"], "v2")
            self.assertEqual(views["raw_orders"]["decision"]["reason"], "source_priority")
            self.assertEqual(views["paid_orders"]["source_version"], "v2")
            self.assertIn("legacy_orders", tombstones)
            self.assertEqual(tombstones["legacy_orders"]["decision"]["reason"], "logical_clock")
            high_value = views["high_value_customers"]
            self.assertEqual(high_value["conversion"]["scale"], 1000)
            self.assertEqual(high_value["conversion"]["rounding"], "floor")
            self.assertEqual(
                high_value["conversion"]["absolute_ms"],
                high_value["conversion"]["base_ms"]
                + high_value["conversion"]["relative_seconds"] * 1000,
            )
            self.assertEqual(
                high_value["original_time"]["kind"],
                "relative_seconds",
            )

    def test_column_invalidation_and_byte_invariance(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            sample = Path(temp_name) / "sample"
            state_db = Path(temp_name) / "state.db"
            generate_sample(sample)
            definitions, _v1, _v2, merged = load_and_merge_sample(sample)
            initialize_state_with_definitions(state_db, definitions, merged)
            events = read_events(sample)
            p001 = next(event for event in events if event["patch_id"] == "patch-0001")
            first = apply_patch_stream(state_db, [p001])
            self.assertEqual(first["failed_count"], 0)
            self.assertEqual(first["plan_generation"], 2)

            before = read_row(state_db, 1, "paid_orders")
            after = read_row(state_db, 2, "paid_orders")
            self.assertEqual(before["plan_json"], after["plan_json"])
            self.assertEqual(before["executable_sql"], after["executable_sql"])

            setup = [
                event
                for event in events
                if event["patch_id"] in {"patch-0002", "patch-0003"}
            ]
            setup_result = apply_patch_stream(state_db, setup)
            self.assertEqual(setup_result["failed_count"], 0)
            self.assertEqual(setup_result["plan_generation"], 4)

            p010 = next(event for event in events if event["patch_id"] == "patch-0010")
            second = apply_patch_stream(state_db, [p010])
            self.assertEqual(second["failed_count"], 0)
            self.assertEqual(second["plan_generation"], 5)
            result = second["results"][0]
            affected = {
                item["view"]: item
                for item in result["invalidation"]["affected_views"]
            }
            self.assertEqual(
                set(affected),
                {"paid_orders", "customer_totals", "daily_orders", "net_orders"},
            )
            self.assertNotIn("customer_labels", affected)
            net_nodes = {
                node["node_id"]
                for node in definitions.views["net_orders"].plan["node_index"]
            }
            self.assertTrue(set(affected["net_orders"]["dirty_nodes"]) < net_nodes)
            connection = sqlite3.connect(state_db)
            try:
                stale = {
                    row[0]
                    for row in connection.execute(
                        "SELECT view_name FROM snapshot_views "
                        "WHERE plan_generation=5 AND stale=1"
                    ).fetchall()
                }
                event_payload = connection.execute(
                    "SELECT path_json, payload_json FROM invalidation_events "
                    "WHERE patch_id='patch-0010' AND view_name='net_orders'"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(stale, {"customer_totals", "daily_orders", "net_orders"})
            self.assertEqual(json.loads(event_payload[0]), ["paid_orders", "net_orders"])
            self.assertEqual(json.loads(event_payload[1])["source"]["sequence"], 10)
            self.assertTrue(verify_state(state_db)["ok"])

    def test_failure_isolation_and_distinct_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            sample = Path(temp_name) / "sample"
            state_db = Path(temp_name) / "state.db"
            generate_sample(sample)
            definitions, _v1, _v2, merged = load_and_merge_sample(sample)
            initialize_state_with_definitions(state_db, definitions, merged)
            events = read_events(sample)
            p004 = next(event for event in events if event["patch_id"] == "patch-0004")
            failed = apply_patch_stream(state_db, [p004])
            self.assertEqual(failed["failed_count"], 1)
            self.assertEqual(failed["plan_generation"], 1)
            error = failed["results"][0]["error"]
            self.assertEqual(error["code"], E_DOWNSTREAM_COLUMN_MISSING)
            self.assertEqual(error["missing_column"], "customer_totals.total_amount")
            self.assertEqual(error["reference_path"][0], "customer_totals")
            self.assertIn(
                error["reference_path"][1],
                {"customer_labels", "high_value_customers"},
            )
            self.assertIsNotNone(error["node_id"])

            p005 = next(event for event in events if event["patch_id"] == "patch-0005")
            succeeded = apply_patch_stream(state_db, [p005])
            self.assertEqual(succeeded["failed_count"], 0)
            self.assertEqual(succeeded["plan_generation"], 2)

            p009 = next(event for event in events if event["patch_id"] == "patch-0009")
            clock_error = apply_patch_stream(state_db, [p009])
            self.assertEqual(
                clock_error["results"][0]["error"]["code"],
                E_LOGIC_CLOCK_REGRESSION,
            )
            status = run_cli("status", "--state-db", str(state_db))
            self.assertEqual(status["plan_generation"], 2)
            self.assertEqual(status["patch_run_counts"]["FAILED"], 2)

    def test_validation_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sample = root / "sample"
            generate_sample(sample)
            definitions = load_view_definitions(sample / "view_definitions.json")

            with self.assertRaises(BusinessError) as syntax_error:
                parse_sql_plan("broken", "SELECT FROM orders_raw", definitions.source_schemas())
            self.assertEqual(syntax_error.exception.code, E_SQL_SYNTAX)

            with self.assertRaises(BusinessError) as column_error:
                parse_sql_plan(
                    "broken",
                    "SELECT missing_column FROM orders_raw",
                    definitions.source_schemas(),
                )
            self.assertEqual(column_error.exception.code, E_COLUMN_UNKNOWN)

            with self.assertRaises(BusinessError) as cycle_error:
                build_view_model(
                    sources={"base": ["x"]},
                    sql_map={
                        "a": "SELECT x FROM b",
                        "b": "SELECT x FROM a",
                    },
                )
            self.assertEqual(cycle_error.exception.code, E_DEPENDENCY_CYCLE)
            self.assertTrue(cycle_error.exception.details["dependency_path"])

            v1_path = sample / "v1_snapshot.json"
            v1 = json.loads(v1_path.read_text(encoding="utf-8"))
            v1["column_map"][0]["upstream_column"] = "does_not_exist"
            broken_v1 = root / "broken_v1.json"
            broken_v1.write_text(canonical_json(v1), encoding="utf-8")
            with self.assertRaises(BusinessError) as v1_error:
                load_v1_snapshot(broken_v1, definitions)
            self.assertEqual(v1_error.exception.code, E_V1_COLUMN_UNKNOWN)

            hash_sample = root / "hash_sample"
            shutil.copytree(sample, hash_sample)
            shard = hash_sample / "v2_shards" / "shard-0002.json"
            shard.write_bytes(shard.read_bytes() + b" ")
            with self.assertRaises(BusinessError) as hash_error:
                load_v2_snapshot(
                    hash_sample / "v2_shards",
                    hash_sample / "sqlite_meta.db",
                    definitions,
                )
            self.assertEqual(hash_error.exception.code, E_SHARD_HASH_MISMATCH)

            generation_sample = root / "generation_sample"
            shutil.copytree(sample, generation_sample)
            connection = sqlite3.connect(generation_sample / "sqlite_meta.db")
            try:
                connection.execute(
                    "UPDATE shards SET plan_generation=2 WHERE shard_id='shard-0002'"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(BusinessError) as generation_error:
                load_v2_snapshot(
                    generation_sample / "v2_shards",
                    generation_sample / "sqlite_meta.db",
                    definitions,
                )
            self.assertEqual(
                generation_error.exception.code,
                E_PLAN_GENERATION_REGRESSION,
            )

            meta_sample = root / "meta_sample"
            shutil.copytree(sample, meta_sample)
            connection = sqlite3.connect(meta_sample / "sqlite_meta.db")
            try:
                connection.execute(
                    "UPDATE view_meta SET column_dependencies_json='{}' "
                    "WHERE canonical_name='paid_orders'"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(BusinessError) as meta_error:
                load_v2_snapshot(
                    meta_sample / "v2_shards",
                    meta_sample / "sqlite_meta.db",
                    definitions,
                )
            self.assertEqual(meta_error.exception.code, E_V2_META_MISMATCH)

    def test_resume_after_staged_and_prepared_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sample = root / "sample"
            generate_sample(sample)
            for crash_point, expected_exit in (
                ("GSB_CRASH_AFTER_STAGE", 98),
                ("GSB_CRASH_AFTER_PREPARE", 97),
            ):
                state_db = root / f"{crash_point}.db"
                run_cli("load", "--sample-dir", str(sample), "--state-db", str(state_db))
                run_cli(
                    "patch",
                    "--state-db",
                    str(state_db),
                    "--events-file",
                    str(sample / "patch_events.jsonl"),
                    "--event-id",
                    "patch-0001",
                    env={crash_point: "1"},
                    expect_code=expected_exit,
                )
                status_before = run_cli("status", "--state-db", str(state_db))
                self.assertEqual(status_before["plan_generation"], 1)
                self.assertEqual(len(status_before["pending_runs"]), 1)
                self.assertEqual(status_before["invalidation_event_count"], 0)
                self.assertFalse(
                    run_cli(
                        "verify",
                        "--state-db",
                        str(state_db),
                        expect_code=2,
                    )["ok"]
                )
                resumed = run_cli("resume", "--state-db", str(state_db))
                self.assertEqual(resumed["recovered_count"], 1)
                status_after = run_cli("status", "--state-db", str(state_db))
                self.assertEqual(status_after["plan_generation"], 2)
                self.assertEqual(status_after["invalidation_event_count"], 1)
                self.assertTrue(
                    run_cli("verify", "--state-db", str(state_db))["ok"]
                )
                duplicate = run_cli(
                    "patch",
                    "--state-db",
                    str(state_db),
                    "--events-file",
                    str(sample / "patch_events.jsonl"),
                    "--event-id",
                    "patch-0001",
                )
                self.assertTrue(duplicate["results"][0]["duplicate"])
                self.assertEqual(duplicate["plan_generation"], 2)
                self.assertEqual(
                    run_cli("status", "--state-db", str(state_db))[
                        "invalidation_event_count"
                    ],
                    1,
                )

    def test_multiprocess_idempotent_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            sample = root / "sample"
            state_db = root / "state.db"
            generate_sample(sample)
            run_cli("load", "--sample-dir", str(sample), "--state-db", str(state_db))
            command = [
                sys.executable,
                "-m",
                "gsb_pipeline",
                "patch",
                "--state-db",
                str(state_db),
                "--events-file",
                str(sample / "patch_events.jsonl"),
                "--event-id",
                "patch-0001",
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(4)
            ]
            outputs = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                outputs.append(json.loads(stdout))
            first_commits = [
                result
                for output in outputs
                for result in output["results"]
                if not result["duplicate"]
            ]
            self.assertEqual(len(first_commits), 1)
            self.assertEqual(
                sum(
                    1
                    for output in outputs
                    for result in output["results"]
                    if result["duplicate"]
                ),
                3,
            )
            status = run_cli("status", "--state-db", str(state_db))
            self.assertEqual(status["plan_generation"], 2)
            self.assertEqual(status["patch_run_counts"], {"COMMITTED": 1})
            self.assertEqual(status["invalidation_event_count"], 1)
            self.assertTrue(run_cli("verify", "--state-db", str(state_db))["ok"])


if __name__ == "__main__":
    unittest.main()

