from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from slowplan.engine import Engine
from slowplan.errors import (
    BusinessError,
    E_BAD_SAMPLE,
    E_CLOCK_ROLLBACK,
    E_DB_DIGEST_MISMATCH,
    E_SAMPLE_NOT_MONOTONIC,
    E_SQL_UNNORMALIZABLE,
    E_STAT_VERSION_CONFLICT,
    E_STAT_VERSION_ROLLBACK,
    E_UNKNOWN_STAT_COLUMN,
    E_ZERO_ESTIMATE,
)
from slowplan.normalize import normalize_sql


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

TPL_CUSTOMER = normalize_sql("SELECT * FROM orders WHERE customer_id = 1").template_id
TPL_STATUS = normalize_sql(
    "SELECT * FROM orders WHERE status = 'paid' AND created_at >= 1"
).template_id
TPL_PRODUCTS = normalize_sql("SELECT * FROM products WHERE category_id IN (1)").template_id
TPL_FLAGS = normalize_sql("SELECT * FROM feature_flags WHERE enabled = 1").template_id


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="slowplan-test-")
        self.db = str(Path(self.tempdir.name) / "state.sqlite")
        self.engine = Engine(self.db)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def load(self) -> dict:
        return self.engine.load(
            str(SAMPLES / "sample_db.sqlite"),
            str(SAMPLES / "stats_snapshot.json"),
        )

    def baseline_profiled(self) -> None:
        self.load()
        self.engine.ingest(str(SAMPLES / "slow_queries.json"))
        self.engine.profile()

    def write_json(self, name: str, payload: object) -> str:
        path = Path(self.tempdir.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()


class WorkflowTests(EngineTestCase):
    def test_complete_workflow_and_incremental_reuse(self) -> None:
        self.load()
        ingest = self.engine.ingest(str(SAMPLES / "slow_queries.json"))
        self.assertEqual(ingest["sample_watermark"], 10)
        profile = self.engine.profile()
        self.assertTrue(profile["ok"])
        status_v1 = self.engine.status()
        profile_ids_before = {
            item["template_id"]: item["profile_version"]
            for item in status_v1["templates"]
        }
        conclusions_before = {}
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                """
                SELECT c.template_id, p.conclusion_json
                FROM current_profiles c JOIN profiles p ON p.profile_id=c.profile_id
                """
            ):
                conclusions_before[row["template_id"]] = row["conclusion_json"]
        finally:
            conn.close()

        apply_result = self.engine.apply(str(SAMPLES / "stats_patch_v2.json"))
        self.assertTrue(apply_result["committed"])
        self.assertFalse(apply_result["activated"])
        self.assertEqual(self.engine.status()["active_stats_version"], 1)

        ingest_v2 = self.engine.ingest(str(SAMPLES / "slow_queries_after_refresh.json"))
        self.assertEqual(ingest_v2["active_stats_version"], 2)
        activated = ingest_v2["activated_stats_versions"][0]
        self.assertEqual(
            set(activated["affected_templates"]),
            {TPL_STATUS, TPL_FLAGS},
        )
        status_after_activation = self.engine.status()
        self.assertEqual(status_after_activation["active_stats_version"], 2)
        self.assertEqual(status_after_activation["invalidations"]["total"], 2)
        by_id = {item["template_id"]: item for item in status_after_activation["templates"]}
        self.assertEqual(by_id[TPL_STATUS]["profile_status"], "pending")
        self.assertEqual(by_id[TPL_FLAGS]["profile_status"], "pending")
        self.assertEqual(by_id[TPL_CUSTOMER]["profile_status"], "ready")
        self.assertEqual(by_id[TPL_PRODUCTS]["profile_status"], "ready")
        self.assertEqual(by_id[TPL_CUSTOMER]["profile_stats_version"], 1)
        self.assertEqual(by_id[TPL_PRODUCTS]["profile_stats_version"], 1)
        self.assertEqual(
            by_id[TPL_CUSTOMER]["profile_version"],
            profile_ids_before[TPL_CUSTOMER],
        )
        self.assertEqual(
            by_id[TPL_PRODUCTS]["profile_version"],
            profile_ids_before[TPL_PRODUCTS],
        )
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            for template_id in (TPL_CUSTOMER, TPL_PRODUCTS):
                row = conn.execute(
                    """
                    SELECT p.conclusion_json FROM current_profiles c
                    JOIN profiles p ON p.profile_id=c.profile_id
                    WHERE c.template_id=?
                    """,
                    (template_id,),
                ).fetchone()
                self.assertEqual(row["conclusion_json"], conclusions_before[template_id])
            events = conn.execute(
                "SELECT * FROM invalidations ORDER BY template_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual({event["template_id"] for event in events}, {TPL_STATUS, TPL_FLAGS})
        for event in events:
            triggers = json.loads(event["triggers_json"])
            paths = json.loads(event["paths_json"])
            self.assertTrue(triggers)
            self.assertTrue(paths)
            self.assertTrue(
                any(
                    node["node_type"] == "profile_conclusion"
                    for path in paths
                    for node in path["nodes"]
                )
            )

        final_profile = self.engine.profile()
        self.assertTrue(final_profile["ok"])
        final_status = self.engine.status()
        by_id = {item["template_id"]: item for item in final_status["templates"]}
        self.assertEqual(by_id[TPL_STATUS]["profile_stats_version"], 2)
        self.assertEqual(by_id[TPL_FLAGS]["profile_stats_version"], 2)
        self.assertEqual(final_status["invalidations"]["pending"], 0)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            flags = conn.execute(
                """
                SELECT p.conclusion_json FROM current_profiles c
                JOIN profiles p ON p.profile_id=c.profile_id
                WHERE c.template_id=?
                """,
                (TPL_FLAGS,),
            ).fetchone()
            conclusion = json.loads(flags["conclusion_json"])
            self.assertTrue(conclusion["precision_sensitive"])
            self.assertEqual(len(conclusion["error_source_chain"]), 2)
            customer = conn.execute(
                """
                SELECT p.conclusion_json FROM current_profiles c
                JOIN profiles p ON p.profile_id=c.profile_id
                WHERE c.template_id=?
                """,
                (TPL_CUSTOMER,),
            ).fetchone()
            customer_conclusion = json.loads(customer["conclusion_json"])
            self.assertEqual(customer_conclusion["aggregate"]["sample_count"], 2)
            self.assertEqual(customer_conclusion["aggregate"]["latency_ms"]["p95"], 72.85)
        finally:
            conn.close()
        verify = self.engine.verify()
        self.assertTrue(verify["ok"], verify["failed_checks"])

    def test_clock_activation_before_effective_and_window_assignment(self) -> None:
        self.baseline_profiled()
        before = self.engine.status()
        apply_result = self.engine.apply(str(SAMPLES / "stats_patch_v2.json"))
        self.assertFalse(apply_result["activated"])
        self.assertEqual(self.engine.status()["active_stats_version"], 1)

        pre_effective = self.write_json(
            "pre_effective.json",
            {
                "samples": [
                    {
                        "sample_seq": 11,
                        "collected_at_ms": 2400,
                        "sql": "SELECT * FROM orders WHERE customer_id = 12",
                        "template_id": TPL_CUSTOMER,
                        "duration_ms": 70,
                        "rows_scanned": 10,
                        "rows_returned": 10,
                        "index_hit": "idx_orders_customer",
                        "session_id": "clock",
                    }
                ]
            },
        )
        self.engine.ingest(pre_effective)
        self.assertEqual(self.engine.status()["active_stats_version"], 1)
        self.engine.profile(now_ms=2400)
        self.assertEqual(self.engine.status()["active_stats_version"], 1)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            customer = conn.execute(
                """
                SELECT p.conclusion_json FROM current_profiles c
                JOIN profiles p ON p.profile_id=c.profile_id
                WHERE c.template_id=?
                """,
                (TPL_CUSTOMER,),
            ).fetchone()
        finally:
            conn.close()
        conclusion = json.loads(customer["conclusion_json"])
        self.assertEqual(conclusion["stats_version"], 1)
        self.assertEqual(conclusion["aggregate"]["sample_count"], 4)
        self.assertEqual(
            conclusion["aggregate"]["windows"],
            [
                {"window_start_ms": 1000, "sample_count": 3},
                {"window_start_ms": 2000, "sample_count": 1},
            ],
        )

        crossing = self.write_json(
            "crossing.json",
            {
                "samples": [
                    {
                        "sample_seq": 12,
                        "collected_at_ms": 2500,
                        "sql": "SELECT * FROM orders WHERE status = 'paid' AND created_at >= 2",
                        "template_id": TPL_STATUS,
                        "duration_ms": 150,
                        "rows_scanned": 24,
                        "rows_returned": 24,
                        "index_hit": "idx_orders_status",
                        "session_id": "clock",
                    }
                ]
            },
        )
        self.engine.ingest(crossing)
        status = self.engine.status()
        self.assertEqual(status["active_stats_version"], 2)
        by_id = {item["template_id"]: item for item in status["templates"]}
        self.assertEqual(by_id[TPL_CUSTOMER]["profile_stats_version"], 1)
        self.assertEqual(by_id[TPL_STATUS]["profile_status"], "pending")
        self.assertEqual(status["invalidations"]["total"], 2)
        conn = sqlite3.connect(self.db)
        try:
            assignment = conn.execute(
                "SELECT template_id FROM samples WHERE sample_seq=12"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(assignment, TPL_STATUS)

        digest_before = self.engine.status()["state_digest"]
        rolled = self.write_json(
            "rolled.json",
            {
                "samples": [
                    {
                        "sample_seq": 13,
                        "collected_at_ms": 100,
                        "sql": "SELECT * FROM orders WHERE customer_id = 13",
                        "template_id": TPL_CUSTOMER,
                        "duration_ms": 70,
                        "rows_scanned": 10,
                        "rows_returned": 10,
                        "index_hit": None,
                        "session_id": "clock",
                    }
                ]
            },
        )
        with self.assertRaises(BusinessError) as raised:
            self.engine.ingest(rolled)
        self.assertEqual(raised.exception.code, E_CLOCK_ROLLBACK)
        self.assertEqual(self.engine.status()["state_digest"], digest_before)

    def test_ingest_apply_are_idempotent(self) -> None:
        self.baseline_profiled()
        repeated_profile = self.engine.profile()
        self.assertTrue(repeated_profile["idempotent"])
        first = self.engine.ingest(str(SAMPLES / "slow_queries.json"))
        second = self.engine.ingest(str(SAMPLES / "slow_queries.json"))
        self.assertTrue(first["idempotent"])
        self.assertTrue(second["idempotent"])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 10)
        finally:
            conn.close()
        applied = self.engine.apply(str(SAMPLES / "stats_patch_v2.json"))
        repeated = self.engine.apply(str(SAMPLES / "stats_patch_v2.json"))
        self.assertTrue(applied["committed"])
        self.assertTrue(repeated["idempotent"])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM stats_snapshots").fetchone()[0],
                2,
            )
        finally:
            conn.close()

    def test_zero_estimate_is_partial_failure(self) -> None:
        self.baseline_profiled()
        self.engine.apply(str(SAMPLES / "stats_patch_zero_estimate.json"))
        self.engine.ingest(str(SAMPLES / "slow_queries_after_refresh.json"))
        self.engine.ingest(str(SAMPLES / "slow_queries_zero_case.json"))
        result = self.engine.profile()
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual([error["code"] for error in result["errors"]], [E_ZERO_ESTIMATE])
        self.assertIn(TPL_STATUS, result["profiled_templates"])
        self.assertIn(TPL_PRODUCTS, result["profiled_templates"])
        self.assertNotIn(TPL_CUSTOMER, result["profiled_templates"])
        status = self.engine.status()
        by_id = {item["template_id"]: item for item in status["templates"]}
        self.assertEqual(by_id[TPL_CUSTOMER]["profile_status"], "pending")
        self.assertEqual(by_id[TPL_CUSTOMER]["last_error"]["code"], E_ZERO_ESTIMATE)
        self.assertEqual(by_id[TPL_STATUS]["profile_status"], "ready")
        self.assertTrue(self.engine.verify()["ok"])


class NegativeInputTests(EngineTestCase):
    def test_bad_sql_does_not_change_state(self) -> None:
        self.baseline_profiled()
        before = self.engine.status()["state_digest"]
        with self.assertRaises(BusinessError) as raised:
            self.engine.ingest(str(SAMPLES / "slow_queries_invalid.json"))
        self.assertEqual(raised.exception.code, E_SQL_UNNORMALIZABLE)
        self.assertEqual(self.engine.status()["state_digest"], before)

    def test_bad_database_digest_creates_no_state(self) -> None:
        with self.assertRaises(BusinessError) as raised:
            self.engine.load(
                str(SAMPLES / "sample_db.sqlite"),
                str(SAMPLES / "stats_snapshot_bad_digest.json"),
            )
        self.assertEqual(raised.exception.code, E_DB_DIGEST_MISMATCH)
        self.assertFalse(Path(self.db).exists())

    def test_unknown_column_and_rollback_do_not_change_state(self) -> None:
        self.baseline_profiled()
        with self.assertRaises(BusinessError) as raised:
            self.engine.apply(str(SAMPLES / "stats_patch_rollback.json"))
        self.assertEqual(raised.exception.code, E_STAT_VERSION_ROLLBACK)
        before = self.engine.status()["state_digest"]
        with self.assertRaises(BusinessError) as raised:
            self.engine.apply(str(SAMPLES / "stats_patch_unknown_column.json"))
        self.assertEqual(raised.exception.code, E_UNKNOWN_STAT_COLUMN)
        self.assertEqual(self.engine.status()["state_digest"], before)
        self.engine.apply(str(SAMPLES / "stats_patch_v2.json"))
        before = self.engine.status()["state_digest"]
        with self.assertRaises(BusinessError) as raised:
            self.engine.apply(str(SAMPLES / "stats_patch_rollback.json"))
        self.assertEqual(raised.exception.code, E_STAT_VERSION_ROLLBACK)
        self.assertEqual(self.engine.status()["state_digest"], before)
        with self.assertRaises(BusinessError) as raised:
            self.engine.apply(str(SAMPLES / "stats_patch_conflict.json"))
        self.assertEqual(raised.exception.code, E_STAT_VERSION_CONFLICT)
        self.assertEqual(self.engine.status()["state_digest"], before)

    def test_sample_schema_validation(self) -> None:
        self.load()
        bad_rows = self.write_json(
            "bad_rows.json",
            {
                "samples": [
                    {
                        "sample_seq": 1,
                        "collected_at_ms": 1000,
                        "sql": "SELECT * FROM orders WHERE customer_id = 1",
                        "template_id": TPL_CUSTOMER,
                        "duration_ms": 10,
                        "rows_scanned": 1,
                        "rows_returned": 2,
                        "index_hit": None,
                        "session_id": "x",
                    }
                ]
            },
        )
        with self.assertRaises(BusinessError) as raised:
            self.engine.ingest(bad_rows)
        self.assertEqual(raised.exception.code, E_BAD_SAMPLE)
        duplicate_seq = self.write_json(
            "duplicate_seq.json",
            {
                "samples": [
                    {
                        "sample_seq": 1,
                        "collected_at_ms": 1000,
                        "sql": "SELECT * FROM orders WHERE customer_id = 1",
                        "template_id": TPL_CUSTOMER,
                        "duration_ms": 10,
                        "rows_scanned": 1,
                        "rows_returned": 1,
                        "index_hit": None,
                        "session_id": "x",
                    },
                    {
                        "sample_seq": 1,
                        "collected_at_ms": 1001,
                        "sql": "SELECT * FROM orders WHERE customer_id = 2",
                        "template_id": TPL_CUSTOMER,
                        "duration_ms": 10,
                        "rows_scanned": 1,
                        "rows_returned": 1,
                        "index_hit": None,
                        "session_id": "x",
                    },
                ]
            },
        )
        with self.assertRaises(BusinessError) as raised:
            self.engine.ingest(duplicate_seq)
        self.assertEqual(raised.exception.code, E_SAMPLE_NOT_MONOTONIC)


if __name__ == "__main__":
    unittest.main()
