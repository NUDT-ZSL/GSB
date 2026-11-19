from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SAMPLES = ROOT / "samples"


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [PYTHON, "-m", "alarm_dedup", *args],
        cwd=ROOT,
        check=False,
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def cli_json(*args: str, env: dict[str, str] | None = None) -> dict:
    result = run_cli(*args, env=env)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not return JSON: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc
    payload["_returncode"] = result.returncode
    return payload


def initialize(db: Path) -> dict:
    return cli_json(
        "--db",
        str(db),
        "load",
        "--topology",
        str(SAMPLES / "topology.json"),
        "--window-ms",
        "1000",
    )


def ingest(db: Path) -> dict:
    return cli_json("--db", str(db), "ingest", "--events", str(SAMPLES / "events.json"))


def patch(db: Path, name: str = "patch.json") -> dict:
    return cli_json("--db", str(db), "patch", "--patch", str(SAMPLES / name))


def worker_patch(db: str) -> tuple[int, str]:
    result = run_cli("--db", db, "patch", "--patch", str(SAMPLES / "patch.json"))
    return result.returncode, result.stdout


def worker_ingest(db: str) -> tuple[int, str]:
    result = run_cli("--db", db, "ingest", "--events", str(SAMPLES / "events.json"))
    return result.returncode, result.stdout


def _mixed_worker(item: tuple[str, str]) -> tuple[int, str]:
    kind, db = item
    if kind == "patch":
        return worker_patch(db)
    return worker_ingest(db)


def connection(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def cluster_by(db: Path, service: str, rule: str, contains_seq: int | None = None) -> dict:
    conn = connection(db)
    try:
        rows = conn.execute(
            "SELECT * FROM clusters WHERE result_version = "
            "(SELECT value FROM meta WHERE key = 'current_result_version') "
            "AND service = ? AND rule = ? ORDER BY cluster_key",
            (service, rule),
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["event_seqs"] = json.loads(item["event_seqs_json"])
            item["suppressed_event_seqs"] = json.loads(item["suppressed_event_seqs_json"])
            item["representative"] = (
                json.loads(item["representative_json"]) if item["representative_json"] else None
            )
            if contains_seq is None or contains_seq in item["event_seqs"]:
                return item
        raise AssertionError(f"cluster not found: {service}/{rule}/{contains_seq}")
    finally:
        conn.close()


def candidate_by(db: Path, cluster_key: str) -> dict:
    conn = connection(db)
    try:
        row = conn.execute(
            "SELECT candidates_json FROM candidates WHERE result_version = "
            "(SELECT value FROM meta WHERE key = 'current_result_version') "
            "AND cluster_key = ?",
            (cluster_key,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"candidate set not found: {cluster_key}")
        return json.loads(row[0])
    finally:
        conn.close()


class AlarmDedupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_business_errors_are_distinct_and_leave_state_unchanged(self) -> None:
        cycle = cli_json(
            "--db",
            str(self.db),
            "load",
            "--topology",
            str(SAMPLES / "invalid" / "topology_cycle.json"),
        )
        self.assertEqual(cycle["error_code"], "TOPOLOGY_CYCLE")
        self.assertFalse(self.db.exists())

        loaded = initialize(self.db)
        self.assertEqual(loaded["_returncode"], 0)
        before = cli_json("--db", str(self.db), "status")

        unknown_event = cli_json(
            "--db",
            str(self.db),
            "ingest",
            "--events",
            str(SAMPLES / "invalid" / "events_unknown_instance.json"),
        )
        self.assertEqual(unknown_event["error_code"], "EVENT_UNKNOWN_INSTANCE")

        unknown_service = cli_json(
            "--db",
            str(self.db),
            "patch",
            "--patch",
            str(SAMPLES / "invalid" / "patch_unknown_service.json"),
        )
        self.assertEqual(unknown_service["error_code"], "PATCH_UNKNOWN_SERVICE")

        cycle_patch = cli_json(
            "--db",
            str(self.db),
            "patch",
            "--patch",
            str(SAMPLES / "invalid" / "patch_cycle.json"),
        )
        self.assertEqual(cycle_patch["error_code"], "TOPOLOGY_CYCLE")

        unknown_window = cli_json(
            "--db",
            str(self.db),
            "patch",
            "--patch",
            str(SAMPLES / "invalid" / "patch_unknown_window.json"),
        )
        self.assertEqual(unknown_window["error_code"], "PATCH_UNKNOWN_WINDOW")

        bad_window = cli_json(
            "--db",
            str(self.db),
            "patch",
            "--patch",
            str(SAMPLES / "invalid" / "patch_bad_window.json"),
        )
        self.assertEqual(bad_window["error_code"], "PATCH_INVALID_SILENCE_WINDOW")
        unchanged = cli_json("--db", str(self.db), "status")
        self.assertEqual(unchanged["topology_version"], before["topology_version"])
        self.assertEqual(unchanged["event_watermark"], before["event_watermark"])
        self.assertEqual(unchanged["result_version"], before["result_version"])

        ingest(self.db)
        patch(self.db)
        rollback = cli_json(
            "--db",
            str(self.db),
            "patch",
            "--patch",
            str(SAMPLES / "invalid" / "patch_rollback.json"),
        )
        self.assertEqual(rollback["error_code"], "PATCH_TOPOLOGY_VERSION_ROLLBACK")
        after = cli_json("--db", str(self.db), "status")
        self.assertEqual(after["topology_version"], 2)
        self.assertNotEqual(before["result_version"], after["result_version"])

    def test_flat_topology_and_empty_event_stream(self) -> None:
        flat_json = Path(self.temp.name) / "flat.json"
        flat_json.write_text(
            json.dumps(
                {
                    "topology_version": 1,
                    "services": [
                        {
                            "service": "api",
                            "instance_id": "api-1",
                            "upstream_dependencies": ["store"],
                            "fault_domain": "az-a",
                            "weight": 1.5,
                        },
                        {
                            "service": "store",
                            "instance_id": "store-1",
                            "upstream_dependencies": [],
                            "fault_domain": "az-b",
                            "weight": 2.0,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        empty_json = Path(self.temp.name) / "empty.json"
        empty_json.write_text('{"events":[]}', encoding="utf-8")
        loaded = cli_json(
            "--db",
            str(self.db),
            "load",
            "--topology",
            str(flat_json),
            "--window-ms",
            "1000",
        )
        self.assertEqual(loaded["_returncode"], 0)
        result = cli_json("--db", str(self.db), "ingest", "--events", str(empty_json))
        self.assertEqual(result["accepted_events"], 0)
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_clustering_representatives_and_root_candidates(self) -> None:
        initialize(self.db)
        result = ingest(self.db)
        self.assertEqual(result["accepted_events"], 15)

        search = cluster_by(self.db, "search", "ErrorRate")
        self.assertEqual(search["event_seqs"], [5, 6])
        self.assertEqual(search["representative_event_seq"], 6)
        self.assertEqual(search["suppressed_count"], 0)

        disk = cluster_by(self.db, "db", "DiskFull")
        self.assertEqual(disk["event_seqs"], [9, 10])
        self.assertEqual(disk["representative_event_seq"], 10)
        self.assertEqual(disk["suppressed_count"], 1)

        inventory = cluster_by(self.db, "inventory", "StockMismatch")
        self.assertIsNone(inventory["representative_event_seq"])
        self.assertEqual(inventory["suppressed_count"], 1)

        checkout = cluster_by(self.db, "checkout", "LatencyHigh")
        candidates = candidate_by(self.db, checkout["cluster_key"])
        self.assertEqual(candidates["aggregation_order"][0], "has_own_unsuppressed_alert_desc")
        self.assertEqual(candidates["candidates"][0]["candidate_instance_id"], "db-1")
        self.assertEqual(candidates["candidates"][0]["causal_distance"], 2)
        self.assertIn("observations", candidates["candidates"][0])
        self.assertIn("causal_distance=2", candidates["candidates"][0]["basis_text"])

        verified = cli_json("--db", str(self.db), "verify")
        self.assertTrue(verified["ok"])

    def test_incremental_patch_recomputes_exact_closure_and_reuses_rest(self) -> None:
        initialize(self.db)
        ingest(self.db)
        before_search = cluster_by(self.db, "search", "ErrorRate")
        before_cache_candidates = candidate_by(self.db, cluster_by(self.db, "cache", "EvictionCritical")["cluster_key"])

        patched = patch(self.db)
        self.assertEqual(patched["_returncode"], 0)
        self.assertEqual(patched["topology_version"], 2)

        db_latency = cluster_by(self.db, "db", "LatencyHigh", contains_seq=1)
        self.assertIsNone(db_latency["representative_event_seq"])
        self.assertEqual(db_latency["suppressed_count"], 1)
        self.assertEqual(db_latency["computed_topology_version"], 2)

        checkout = cluster_by(self.db, "checkout", "LatencyHigh")
        checkout_candidates = candidate_by(self.db, checkout["cluster_key"])
        upstream_services = {item["service"] for item in checkout_candidates["upstream_services"]}
        self.assertNotIn("cache", upstream_services)
        self.assertEqual(checkout_candidates["candidates"][0]["candidate_instance_id"], "payment-1")

        search = cluster_by(self.db, "search", "ErrorRate")
        self.assertEqual(search["cluster_key"], before_search["cluster_key"])
        self.assertEqual(search["representative_event_seq"], before_search["representative_event_seq"])
        self.assertEqual(search["computed_topology_version"], 1)
        self.assertEqual(search["reused_from_version"], 2)

        cache_candidates = candidate_by(self.db, cluster_by(self.db, "cache", "EvictionCritical")["cluster_key"])
        self.assertEqual(cache_candidates, before_cache_candidates)

        conn = connection(self.db)
        try:
            run = conn.execute(
                "SELECT operation_id FROM cluster_runs WHERE result_version = 3"
            ).fetchone()
            rows = conn.execute(
                "SELECT item_type, action, COUNT(*) AS count FROM invalidation_log "
                "WHERE operation_id = ? GROUP BY item_type, action ORDER BY item_type, action",
                (run[0],),
            ).fetchall()
            counts = {(row["item_type"], row["action"]): row["count"] for row in rows}
        finally:
            conn.close()
        self.assertGreater(counts[("cluster", "recompute")], 0)
        self.assertGreater(counts[("cluster", "reuse")], 0)
        self.assertGreater(counts[("candidate", "recompute")], 0)
        self.assertGreater(counts[("candidate", "reuse")], 0)

        verified = cli_json("--db", str(self.db), "verify")
        self.assertTrue(verified["ok"])

    def test_window_revoke_is_sticky_and_restart_safe(self) -> None:
        initialize(self.db)
        ingest(self.db)
        patch(self.db)
        suppressed_key = cluster_by(self.db, "db", "LatencyHigh", contains_seq=1)["cluster_key"]
        self.assertIsNone(cluster_by(self.db, "db", "LatencyHigh", contains_seq=1)["representative_event_seq"])

        reverted = patch(self.db, "patch_revert.json")
        self.assertEqual(reverted["topology_version"], 3)
        after_revert = cluster_by(self.db, "db", "LatencyHigh", contains_seq=1)
        self.assertEqual(after_revert["cluster_key"], suppressed_key)
        self.assertIsNone(after_revert["representative_event_seq"])
        self.assertEqual(after_revert["suppressed_count"], 1)
        self.assertEqual(after_revert["computed_topology_version"], 3)

        conn = connection(self.db)
        try:
            row = conn.execute(
                "SELECT suppressed, source, window_id FROM event_suppressions WHERE event_seq = 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["suppressed"], 1)
        self.assertEqual(row["source"], "window_silence")
        self.assertEqual(row["window_id"], "silence-db-latency")
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_idempotent_ingest_and_patch(self) -> None:
        initialize(self.db)
        first_ingest = ingest(self.db)
        second_ingest = ingest(self.db)
        self.assertTrue(second_ingest["idempotent"])
        self.assertEqual(second_ingest["batch_id"], first_ingest["batch_id"])

        first_patch = patch(self.db)
        second_patch = patch(self.db)
        self.assertTrue(second_patch["idempotent"])
        self.assertEqual(second_patch["patch_id"], first_patch["patch_id"])

        conn = connection(self.db)
        try:
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            patches = conn.execute("SELECT COUNT(*) FROM patch_history").fetchone()[0]
            topology_versions = conn.execute("SELECT COUNT(*) FROM topology_versions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(events, 15)
        self.assertEqual(patches, 1)
        self.assertEqual(topology_versions, 2)

    def test_concurrent_duplicate_patch_and_ingest_are_safe(self) -> None:
        initialize(self.db)
        context = multiprocessing.get_context("spawn")
        with context.Pool(2) as pool:
            patch_results = pool.map(worker_patch, [str(self.db), str(self.db)])
        self.assertEqual([code for code, _ in patch_results], [0, 0])
        outputs = [json.loads(text) for _, text in patch_results]
        self.assertEqual(sum(1 for item in outputs if not item.get("idempotent", False)), 1)
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

        with context.Pool(2) as pool:
            ingest_results = pool.map(worker_ingest, [str(self.db), str(self.db)])
        self.assertEqual([code for code, _ in ingest_results], [0, 0])
        outputs = [json.loads(text) for _, text in ingest_results]
        self.assertEqual(sum(1 for item in outputs if not item.get("idempotent", False)), 1)
        conn = connection(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            watermark = conn.execute(
                "SELECT value FROM meta WHERE key = 'event_watermark'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 15)
        self.assertEqual(int(watermark), 15)
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_mixed_patch_and_ingest_run_in_parallel(self) -> None:
        initialize(self.db)
        context = multiprocessing.get_context("spawn")
        with context.Pool(2) as pool:
            results = pool.map(
                _mixed_worker,
                [
                    ("patch", str(self.db)),
                    ("ingest", str(self.db)),
                ],
            )
        self.assertEqual([code for code, _ in results], [0, 0])
        status = cli_json("--db", str(self.db), "status")
        self.assertEqual(status["topology_version"], 2)
        self.assertEqual(status["event_watermark"], 15)
        self.assertEqual(status["cluster_count"], 12)
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_crash_recovery_replays_only_pending_job(self) -> None:
        initialize(self.db)
        crashed = run_cli(
            "--db",
            str(self.db),
            "ingest",
            "--events",
            str(SAMPLES / "events.json"),
            env={"ALARM_DEDUP_TEST_CRASH_POINT": "job_enqueued"},
        )
        self.assertEqual(crashed.returncode, 91)
        pending = cli_json("--db", str(self.db), "status")
        self.assertEqual(pending["cluster_count"], 0)
        self.assertEqual(pending["event_watermark"], 0)
        self.assertEqual(len(pending["pending_jobs"]), 1)

        resumed = cli_json("--db", str(self.db), "resume")
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["replayed_count"], 1)
        final = cli_json("--db", str(self.db), "status")
        self.assertEqual(final["event_watermark"], 15)
        self.assertEqual(final["pending_jobs"], [])
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_crash_before_commit_rolls_back_and_resumes(self) -> None:
        initialize(self.db)
        crashed = run_cli(
            "--db",
            str(self.db),
            "ingest",
            "--events",
            str(SAMPLES / "events.json"),
            env={"ALARM_DEDUP_TEST_CRASH_POINT": "before_commit"},
        )
        self.assertEqual(crashed.returncode, 91)
        intermediate = cli_json("--db", str(self.db), "status")
        self.assertEqual(intermediate["event_watermark"], 0)
        self.assertEqual(intermediate["cluster_count"], 0)
        self.assertEqual(len(intermediate["pending_jobs"]), 1)
        resumed = cli_json("--db", str(self.db), "resume")
        self.assertTrue(resumed["ok"])
        self.assertTrue(cli_json("--db", str(self.db), "verify")["ok"])

    def test_cli_pipeline_is_bitwise_deterministic(self) -> None:
        def run_pipeline(directory: Path) -> list[str]:
            directory.mkdir()
            db = directory / "state.db"
            outputs = []
            commands = [
                ("--db", str(db), "load", "--topology", str(SAMPLES / "topology.json"), "--window-ms", "1000"),
                ("--db", str(db), "ingest", "--events", str(SAMPLES / "events.json")),
                ("--db", str(db), "status"),
                ("--db", str(db), "patch", "--patch", str(SAMPLES / "patch.json")),
                ("--db", str(db), "status"),
                ("--db", str(db), "patch", "--patch", str(SAMPLES / "patch_revert.json")),
                ("--db", str(db), "status"),
                ("--db", str(db), "verify"),
            ]
            for command in commands:
                result = run_cli(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outputs.append(result.stdout)
            return outputs

        first = run_pipeline(Path(self.temp.name) / "first")
        second = run_pipeline(Path(self.temp.name) / "second")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
