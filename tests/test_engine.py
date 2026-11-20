from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from gsb.aggregation import aggregate_batch_partial
from gsb.errors import GSBMemoryBudgetError
from gsb.runner import CrashInjector, Engine
from gsb.validation import load_sample


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = load_sample(Path(__file__).resolve().parents[1] / "samples" / "tilted")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_engine(self, tag: str) -> tuple[dict, dict]:
        db = self.root / f"{tag}.sqlite"
        work = self.root / f"{tag}-work"
        out = self.root / f"{tag}-out"
        with Engine(self.sample, db, work, out, CrashInjector(None)) as engine:
            reports = {}
            for query in self.sample.queries:
                reports[query.query_id] = engine.process_query(query, "test")
            verify = engine.verify_all()
        return reports, verify

    def digest(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_end_to_end_hotspot_budget_spill_and_consistency(self) -> None:
        reports, verify = self.run_engine("first")
        self.assertTrue(verify["ok"])
        main = reports["tilted-main"]
        self.assertEqual(main["status"], "complete")
        self.assertEqual(main["mode"]["final_mode"], "hot_split_two_phase")
        self.assertGreaterEqual(main["mode"]["switch_count"], 1)
        self.assertTrue(main["latency"]["within_budget"])
        self.assertFalse(main["latency"]["wall_clock_used"])
        self.assertEqual(main["hotspot_analysis"]["hot_keys"], ["hot-A", "hot-B"])
        self.assertEqual(
            [item["trigger_batch_index"] for item in main["hotspot_analysis"]["changes"]],
            [2, 6],
        )
        self.assertGreater(main["spill_and_memory"]["spill_count"], 0)
        self.assertGreater(main["spill_and_memory"]["readback_count"], 0)
        self.assertTrue(main["consistency"]["ok"])
        self.assertFalse(main["consistency"]["float_tolerance_applied"])
        self.assertEqual(main["state"]["group_count"], 63)
        result_path = Path(main["result_path"])
        result = json.loads(result_path.read_text())
        hot_a = [item for item in result["groups"] if item["key"] == "hot-A"][0]
        self.assertTrue(
            hot_a["aggregates"]["amount_sum"]["value"] != hot_a["aggregates"]["amount_sum"]["value"]
        )

    def test_two_fresh_runs_are_byte_identical(self) -> None:
        first, verify_first = self.run_engine("run-a")
        second, verify_second = self.run_engine("run-b")
        self.assertTrue(verify_first["ok"])
        self.assertTrue(verify_second["ok"])
        for query_id in ("tilted-main", "tilted-window"):
            self.assertEqual(
                self.digest(Path(first[query_id]["result_path"])),
                self.digest(Path(second[query_id]["result_path"])),
            )
            self.assertEqual(
                first[query_id]["result_digest"],
                second[query_id]["result_digest"],
            )

    def test_completed_query_is_idempotent_on_rerun(self) -> None:
        db = self.root / "idempotent.sqlite"
        work = self.root / "idempotent-work"
        out = self.root / "idempotent-out"
        query = self.sample.query("tilted-main")
        with Engine(self.sample, db, work, out) as engine:
            first = engine.process_query(query, "one")
            versions_first = engine.storage.connection.execute(
                "SELECT COUNT(*) FROM group_state_versions WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()[0]
            second = engine.process_query(query, "two")
            versions_second = engine.storage.connection.execute(
                "SELECT COUNT(*) FROM group_state_versions WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()[0]
        self.assertEqual(first["result_digest"], second["result_digest"])
        self.assertEqual(versions_first, versions_second)
        self.assertEqual(versions_first, 504)

    def test_half_committed_group_ledger_is_repaired(self) -> None:
        db = self.root / "half.sqlite"
        work = self.root / "half-work"
        out = self.root / "half-out"
        query = self.sample.query("tilted-main")
        batch = self.sample.batches[0]
        with Engine(self.sample, db, work, out) as engine:
            engine.storage.initialize_query(self.sample, query)
            partial = aggregate_batch_partial(batch, query)
            engine.storage.stage_batch(query, batch, partial, "test")
            token = sorted(partial, key=lambda item: partial[item]["sort_key"])[0]
            with engine.storage.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO group_state_versions(
                        query_id, ordinal, batch_index, group_token,
                        version, delta_digest
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        query.query_id,
                        batch.ordinal,
                        batch.batch_index,
                        token,
                        batch.ordinal,
                        "half-commit",
                    ),
                )
            recovered = engine.storage.recover_prepared(query)
            self.assertEqual(recovered["half_committed_repaired"], 1)
            rows = engine.storage.connection.execute(
                "SELECT COUNT(*) FROM group_state_versions WHERE query_id = ?",
                (query.query_id,),
            ).fetchone()[0]
            self.assertEqual(rows, 0)
            report = engine.process_query(query, "test")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["recovery"]["half_committed_repaired"], 1)

    def test_memory_lease_prevents_oversell(self) -> None:
        db = self.root / "lease.sqlite"
        work = self.root / "lease-work"
        out = self.root / "lease-out"
        with Engine(self.sample, db, work, out) as engine:
            query = self.sample.query("tilted-main")
            engine.storage.initialize_query(self.sample, query)
            lease = engine.storage.acquire_memory_lease(
                query.query_id, 10, 10
            )
            with self.assertRaises(GSBMemoryBudgetError):
                engine.storage.acquire_memory_lease(
                    query.query_id,
                    1,
                    10,
                    timeout_seconds=0.05,
                )
            engine.storage.release_memory_lease(lease)
            lease_again = engine.storage.acquire_memory_lease(
                query.query_id, 10, 10
            )
            engine.storage.release_memory_lease(lease_again)

    def test_spill_and_no_spill_runs_have_identical_results(self) -> None:
        baseline, _ = self.run_engine("spill-baseline")
        modified_root = self.root / "no-spill-sample"
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "samples" / "tilted",
            modified_root,
        )
        config_path = modified_root / "config.json"
        config = json.loads(config_path.read_text())
        config["memory_limit_mib"] = 1.0
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        modified = load_sample(modified_root)
        with Engine(
            modified,
            self.root / "no-spill.sqlite",
            self.root / "no-spill-work",
            self.root / "no-spill-out",
        ) as engine:
            report = engine.process_query(modified.query("tilted-main"), "test")
        self.assertEqual(report["spill_and_memory"]["spill_count"], 0)
        self.assertEqual(
            report["result_digest"],
            baseline["tilted-main"]["result_digest"],
        )
        self.assertEqual(
            self.digest(Path(report["result_path"])),
            self.digest(Path(baseline["tilted-main"]["result_path"])),
        )


if __name__ == "__main__":
    unittest.main()

