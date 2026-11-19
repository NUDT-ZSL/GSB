from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mvcc_app.engine import Engine
from mvcc_app.store import Store
from tests.helpers import sample_config, sample_sessions, sample_table


def _sample_engine(directory: Path) -> tuple[Engine, Store]:
    config = sample_config()
    config["db_path"] = str(directory / "mvcc.db")
    config["store_dir"] = str(directory / "versions")
    sessions = sample_sessions()
    store = Store(config["db_path"])
    store.initialize(sample_table(), config, sessions)
    engine = Engine(store)
    engine.replay_sessions(sessions)
    return engine, store


class GCTests(unittest.TestCase):
    def test_gc_preserves_versions_visible_to_retained_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, store = _sample_engine(Path(directory))
            _, before = store.load_head()
            before_rc = engine.read(
                109,
                clock_ms=2010,
                range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
            )
            before_rr = engine.read(
                108,
                clock_ms=2010,
                range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
            )
            result = engine.run_gc(clock_ms=2200)
            self.assertGreater(result["collected_count"], 0)
            _, after = store.load_head()
            after_rc = engine.read(
                109,
                clock_ms=2210,
                range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
            )
            after_rr = engine.read(
                108,
                clock_ms=2210,
                range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
            )
            self.assertEqual(
                [item["values"] for item in before_rc["rows"]],
                [item["values"] for item in after_rc["rows"]],
            )
            self.assertEqual(
                [item["visibility"]["txn_chain"] for item in before_rr["rows"]],
                [item["visibility"]["txn_chain"] for item in after_rr["rows"]],
            )
            for view_id, view in after["read_views"].items():
                if view["status"] != "retained" or view_id not in before["read_views"]:
                    continue
                for row_key in {item["row_key"] for item in before["versions"].values()}:
                    visible_before = engine._find_visible_version(before, view, row_key)
                    if visible_before is not None:
                        self.assertIn(visible_before["version_id"], after["versions"])
            status = engine.status()
            self.assertEqual(status["gc"]["collected_total"], result["collected_count"])
            self.assertIsNotNone(status["gc"]["last"])
            self.assertTrue(status["gc"]["last"]["retained"])
            self.assertEqual(
                sum(
                    item["collected_count"]
                    for item in after["gc_runs"].values()
                ),
                after["gc"]["collected_total"],
            )

    def test_concurrent_gc_and_queries_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine, store = _sample_engine(Path(directory))
            engine.read(
                108,
                clock_ms=2010,
                range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
            )
            barrier = threading.Barrier(3)

            def collect() -> dict:
                barrier.wait()
                return engine.run_gc(clock_ms=2020)

            def rr_query() -> dict:
                barrier.wait()
                return engine.read(
                    108,
                    clock_ms=2020,
                    range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
                )

            def rc_query() -> dict:
                barrier.wait()
                return engine.read(
                    109,
                    clock_ms=2020,
                    range_raw={"where": [{"column": "id", "op": "eq", "value": 3}]},
                )

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=3) as executor:
                gc_future = executor.submit(collect)
                rr_future = executor.submit(rr_query)
                rc_future = executor.submit(rc_query)
                gc_result = gc_future.result(timeout=10)
                rr_result = rr_future.result(timeout=10)
                rc_result = rc_future.result(timeout=10)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 5.0)
            self.assertGreater(gc_result["collected_count"], 0)
            self.assertEqual(rr_result["rows"][0]["values"]["balance"], 350)
            self.assertEqual(rc_result["rows"][0]["values"]["balance"], 375)
            self.assertLessEqual(
                rr_result["logical_latency_ms"],
                rr_result["latency_budget_ms"],
            )
            self.assertLessEqual(
                rc_result["logical_latency_ms"],
                rc_result["latency_budget_ms"],
            )
            integrity = store.inspect_integrity()
            self.assertTrue(integrity["ok"], integrity)


if __name__ == "__main__":
    unittest.main()

