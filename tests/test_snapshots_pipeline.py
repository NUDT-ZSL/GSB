import json
import os
import tempfile
import unittest

from layout_order.config import AppConfig
from layout_order.errors import (
    E_INCONSISTENT,
    E_SNAPSHOT_CORRUPT,
    LayoutOrderError,
)
from layout_order.generator import (
    baseline_specs,
    generate_page,
    write_jsonl,
)
from layout_order.geometry import load_jsonl
from layout_order.pipeline import cross_check_page, process_page
from layout_order.snapshots import CacheLedger, SnapshotStore, checksum_of


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SnapshotStore(os.path.join(self.tmp.name, "snapshots"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_version_monotonic_and_unchanged_payload_reuses_version(self) -> None:
        first = self.store.put("order_result", "p1", {"order": ["a", "b"]})
        again = self.store.put("order_result", "p1", {"order": ["a", "b"]})
        second = self.store.put("order_result", "p1", {"order": ["b", "a"]})
        self.assertEqual(first.version, 1)
        self.assertEqual(again.version, 1)
        self.assertEqual(second.version, 2)
        loaded = self.store.get("order_result", "p1")
        self.assertEqual(loaded.checksum, checksum_of({"order": ["b", "a"]}))

    def test_corrupted_checksum_raises_snapshot_corrupt(self) -> None:
        self.store.put("layout_stats", "p1", {"n_blocks": 5})
        path = next(
            os.path.join(self.store.directory, name)
            for name in os.listdir(self.store.directory)
            if name.startswith("layout_stats")
        )
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
        envelope["payload"]["n_blocks"] = 999
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle)
        with self.assertRaises(LayoutOrderError) as caught:
            self.store.get("layout_stats", "p1")
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(caught.exception.details["category"], "checksum_mismatch")

    def test_malformed_envelope_raises_snapshot_corrupt(self) -> None:
        self.store.put("cost_weights", "k1", {"w_gap": 1.0})
        path = next(
            os.path.join(self.store.directory, name)
            for name in os.listdir(self.store.directory)
            if name.startswith("cost_weights")
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not-json")
        with self.assertRaises(LayoutOrderError) as caught:
            self.store.get("cost_weights", "k1")
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)


class CacheLedgerTests(unittest.TestCase):
    def test_reuse_and_recompute_records_correspond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CacheLedger(os.path.join(tmp, "cache"))
            calls = {"count": 0}

            def builder():
                calls["count"] += 1
                return {"edges": ["e1"]}

            payload, first_record = ledger.get_or_build("p1", "cfg", "sum1", builder)
            _, second_record = ledger.get_or_build("p1", "cfg", "sum1", builder)
            recompute_record = ledger.record_recompute("p1", "cfg")
            self.assertEqual(first_record["action"], "build")
            self.assertEqual(second_record["action"], "reuse")
            self.assertEqual(recompute_record["action"], "recompute")
            self.assertEqual(calls["count"], 1)
            self.assertEqual(payload["edges"], ["e1"])
            summary = ledger.summary()
            self.assertEqual(summary["builds"], 1)
            self.assertEqual(summary["reuses"], 1)
            self.assertEqual(summary["recomputes"], 1)

    def test_cache_checksum_mismatch_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CacheLedger(os.path.join(tmp, "cache"))
            ledger.get_or_build("p1", "cfg", "sum1", lambda: {"edges": []})
            with self.assertRaises(LayoutOrderError) as caught:
                ledger.get_or_build("p1", "cfg", "wrong", lambda: {"edges": []})
            self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(
                caught.exception.details["category"], "cache_checksum_mismatch"
            )


class PipelineTests(unittest.TestCase):
    def test_end_to_end_page_report_and_cross_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = os.path.join(tmp, "baseline.jsonl")
            write_jsonl(baseline_specs()[:2], dataset)
            pages = load_jsonl(dataset)
            config = AppConfig()
            store = SnapshotStore(os.path.join(tmp, "snapshots"))
            ledger = CacheLedger(os.path.join(tmp, "cache", "baseline"))
            for page in pages:
                report = process_page(page, config, store, ledger)
                checks = cross_check_page(page, report, config)
                self.assertTrue(all(checks.values()))
                self.assertEqual(
                    report["decision"]["selected_path"], report["result"]["path"]
                )
                self.assertTrue(report["metrics"]["reference"]["reference_available"])

    def test_second_run_reuses_cache_and_stays_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = os.path.join(tmp, "baseline.jsonl")
            write_jsonl(baseline_specs()[:1], dataset)
            page = load_jsonl(dataset)[0]
            config = AppConfig()
            store = SnapshotStore(os.path.join(tmp, "snapshots"))
            ledger = CacheLedger(os.path.join(tmp, "cache", "baseline"))
            first = process_page(page, config, store, ledger)
            second = process_page(page, config, store, ledger)
            self.assertEqual(
                first["result"]["order"], second["result"]["order"]
            )
            self.assertEqual(ledger.summary()["reuses"], 1)
            self.assertEqual(ledger.summary()["recomputes"], 2)

    def test_tampered_report_order_triggers_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dataset = os.path.join(tmp, "baseline.jsonl")
            write_jsonl(baseline_specs()[:1], dataset)
            page = load_jsonl(dataset)[0]
            config = AppConfig()
            store = SnapshotStore(os.path.join(tmp, "snapshots"))
            ledger = CacheLedger(os.path.join(tmp, "cache", "baseline"))
            report = process_page(page, config, store, ledger)
            report["result"]["order"].reverse()
            with self.assertRaises(LayoutOrderError) as caught:
                cross_check_page(page, report, config)
            self.assertEqual(caught.exception.code, E_INCONSISTENT)


if __name__ == "__main__":
    unittest.main()
