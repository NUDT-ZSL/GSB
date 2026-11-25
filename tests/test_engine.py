from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from panoptic_audit.audit import read_report
from panoptic_audit.errors import AuditError, ErrorCode
from panoptic_audit.execution import evaluate_degradation
from panoptic_audit.groundtruth import ParsedTruth
from panoptic_audit.ledger import build_unified_ledger, conflict_policy
from panoptic_audit.parsers import parse_rle, parse_v1, parse_v2
from panoptic_audit.pipeline import regenerate_audit, run_audit
from panoptic_audit.probes import ProbePriorityQueue, parse_probe_plan
from panoptic_audit.sample import generate_sample
from panoptic_audit.selfcheck import cross_module_check
from panoptic_audit.store import LedgerStore


class SampleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample_dir = self.root / "sample"
        self.manifest = generate_sample(self.sample_dir)
        self.entities = {(e["category_id"], e["instance_id"]) for e in self.manifest["entities"]}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self, **overrides) -> Path:
        config = {
            "probe_time_budget_ms": 50.0,
            "conflict_coverage_threshold": 0.5,
            "format_priority": ["RLE", "v2", "v1"],
            "ledger_version": 1,
            "audit_output_path": str(self.root / "artifacts" / "audit_report.json"),
            "sample_dir": str(self.sample_dir),
            "ledger_path": str(self.root / "artifacts" / "ledger.sqlite3"),
            "high_water_path": str(self.root / "artifacts" / "ledger.hwm.json"),
            "probe_round_size": 2,
            "slow_budget_share_threshold": 0.35,
            "confidence_fast_threshold": 0.75,
        }
        config.update(overrides)
        path = self.root / "run_config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path


class FormatParserTests(SampleTestBase):
    def test_three_formats_parse_and_share_shape(self) -> None:
        v1 = parse_v1(self.sample_dir / "gt_v1.json", self.entities)
        v2 = parse_v2(self.sample_dir / "gt_v2")
        rle = parse_rle(self.sample_dir / "gt_rle.json", self.entities)
        self.assertEqual((v1.height, v1.width), (16, 16))
        self.assertEqual(v2.category.shape, (16, 16))
        self.assertEqual(rle.instance.shape, (16, 16))
        self.assertTrue(any(block["block_id"] == "v1:row:0" for block in v1.blocks))
        self.assertTrue(any(block["block_id"] == "rle:category" for block in rle.blocks))

    def test_unknown_version_returns_unsupported(self) -> None:
        path = self.root / "bad_v1.json"
        payload = json.loads((self.sample_dir / "gt_v1.json").read_text())
        payload["version"] = "v9"
        path.write_text(json.dumps(payload))
        with self.assertRaises(AuditError) as ctx:
            parse_v1(path)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_UNSUPPORTED)

    def test_missing_field_returns_corrupt(self) -> None:
        path = self.root / "missing_v1.json"
        payload = json.loads((self.sample_dir / "gt_v1.json").read_text())
        del payload["rows"]
        path.write_text(json.dumps(payload))
        with self.assertRaises(AuditError) as ctx:
            parse_v1(path)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)

    def test_checksum_tamper_returns_corrupt(self) -> None:
        path = self.root / "tampered_v1.json"
        payload = json.loads((self.sample_dir / "gt_v1.json").read_text())
        payload["rows"][4]["identities"][3] += 1
        path.write_text(json.dumps(payload))
        with self.assertRaises(AuditError) as ctx:
            parse_v1(path, self.entities)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)

    def test_v2_unknown_sqlite_version_returns_unsupported(self) -> None:
        db = self.sample_dir / "gt_v2" / "gt_v2_meta.sqlite"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE format_meta SET value='v9' WHERE key='version'")
        conn.commit()
        conn.close()
        with self.assertRaises(AuditError) as ctx:
            parse_v2(self.sample_dir / "gt_v2")
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_UNSUPPORTED)

    def test_rle_checksum_tamper_returns_corrupt(self) -> None:
        path = self.root / "tampered_rle.json"
        payload = json.loads((self.sample_dir / "gt_rle.json").read_text())
        payload["channels"]["category"]["checksum"] = "sha256:bad"
        path.write_text(json.dumps(payload))
        with self.assertRaises(AuditError) as ctx:
            parse_rle(path)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)


class ProbePlanTests(unittest.TestCase):
    def test_duplicates_priority_and_tie_order(self) -> None:
        payload = {
            "format": "active_probe_plan",
            "version": "plan-v1",
            "probes": [
                {"probe_id": "low-dup", "x": 1, "y": 1, "expected_category": 1, "expected_instance": 1, "priority": 2, "time_budget_ms": 1},
                {"probe_id": "high-dup", "x": 1, "y": 1, "expected_category": 1, "expected_instance": 1, "priority": 9, "time_budget_ms": 1},
                {"probe_id": "tie-a", "x": 2, "y": 2, "expected_category": 1, "expected_instance": 1, "priority": 4, "time_budget_ms": 1},
                {"probe_id": "tie-b", "x": 3, "y": 3, "expected_category": 1, "expected_instance": 1, "priority": 4, "time_budget_ms": 1},
                {"probe_id": "bad", "x": 99, "y": 1, "expected_category": 1, "expected_instance": 1, "priority": 1, "time_budget_ms": 1},
            ],
        }
        accepted, rejected, suppressed = parse_probe_plan(payload, 16, 16)
        self.assertEqual([p.probe_id for p in accepted], ["high-dup", "tie-a", "tie-b"])
        queue = ProbePriorityQueue(accepted)
        self.assertEqual([queue.pop().probe_id for _ in range(len(accepted))], ["high-dup", "tie-a", "tie-b"])
        self.assertEqual(suppressed[0]["probe_id"], "low-dup")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["code"], ErrorCode.PLAN_INVALID)


class MergeTests(SampleTestBase):
    def test_priority_breaks_exact_weight_tie(self) -> None:
        def make_truth(fmt: str, category: int, weight: float) -> ParsedTruth:
            return ParsedTruth(
                fmt, "test", 1, 1,
                __import__("numpy").array([[category]], dtype="int64"),
                __import__("numpy").array([[1]], dtype="int64"),
                __import__("numpy").array([[False]]),
                __import__("numpy").array([[weight]]),
                __import__("numpy").array([[weight]]),
            )

        truths = {
            "RLE": make_truth("RLE", 1, 0.9),
            "v2": make_truth("v2", 2, 0.9),
            "v1": make_truth("v1", 3, 0.1),
        }
        ledger = build_unified_ledger(truths, ["RLE", "v2", "v1"])
        pixel = ledger.pixel_at(0, 0)
        self.assertEqual(pixel["winner"]["category"], 1)
        self.assertTrue(pixel["tie_broken_by_priority"])
        self.assertEqual(pixel["sources_hit"], ["RLE"])

    def test_conflict_policy_reports_first_position(self) -> None:
        v1 = parse_v1(self.sample_dir / "gt_v1.json", self.entities)
        v2 = parse_v2(self.sample_dir / "gt_v2")
        rle = parse_rle(self.sample_dir / "gt_rle.json", self.entities)
        ledger = build_unified_ledger({"v1": v1, "v2": v2, "RLE": rle}, ["RLE", "v2", "v1"])
        summary = conflict_policy(ledger, 10, 0.5)
        self.assertFalse(summary["exceeded"])
        self.assertEqual(summary["first_conflict"], {"x": 15, "y": 1})
        with self.assertRaises(AuditError) as ctx:
            conflict_policy(ledger, 10, 0.0)
        self.assertEqual(ctx.exception.code, ErrorCode.GT_SEMANTIC_CONFLICT)
        self.assertEqual(ctx.exception.details["first_conflict"], {"x": 15, "y": 1})


class SnapshotTests(SampleTestBase):
    def _ledger_and_sources(self):
        v1 = parse_v1(self.sample_dir / "gt_v1.json", self.entities)
        v2 = parse_v2(self.sample_dir / "gt_v2")
        rle = parse_rle(self.sample_dir / "gt_rle.json", self.entities)
        truths = {"RLE": rle, "v2": v2, "v1": v1}
        ledger = build_unified_ledger(truths, ["RLE", "v2", "v1"])
        from panoptic_audit.parsers.formats import iter_source_checksums
        return ledger, sorted(iter_source_checksums(truths.values()), key=lambda x: (x["format"], x["block_id"]))

    @staticmethod
    def record(uid: str, sequence: int, ns: int = 100, speed: str = "fast") -> dict:
        return {
            "uid": uid,
            "sequence": sequence,
            "probe_id": f"P-{sequence}",
            "timing": {"charged_ns": ns},
            "execution": {"speed_class": speed},
        }

    def test_atomic_commit_reopen_and_duplicate_guard(self) -> None:
        ledger, sources = self._ledger_and_sources()
        db = self.root / "ledger.sqlite3"
        hwm = self.root / "hwm.json"
        store = LedgerStore.create(db, ledger_version=1, adjudicated=ledger, source_checksums=sources, high_water_path=hwm)
        store.commit_round([self.record("0,0", 0)], budget_ns=1000, degraded=False)
        store.commit_round([self.record("1,1", 1)], budget_ns=1000, degraded=False)
        self.assertEqual(store.head()["revision"], 2)
        with self.assertRaises(AuditError) as ctx:
            store.commit_round([self.record("0,0", 0)], budget_ns=1000, degraded=False)
        self.assertEqual(ctx.exception.code, ErrorCode.INCONSISTENT)
        store.close()

        reopened = LedgerStore.open(db, hwm)
        self.assertEqual(len(reopened.load_probe_records()), 2)
        self.assertEqual(reopened.load_progress()["spent_ns"], 200)
        reopened.close()

    def test_rollback_detection(self) -> None:
        ledger, sources = self._ledger_and_sources()
        db = self.root / "ledger.sqlite3"
        hwm = self.root / "hwm.json"
        backup = self.root / "rev1.sqlite3"
        store = LedgerStore.create(db, ledger_version=1, adjudicated=ledger, source_checksums=sources, high_water_path=hwm)
        store.commit_round([self.record("0,0", 0)], budget_ns=1000, degraded=False)
        store.close()
        shutil.copyfile(db, backup)

        store = LedgerStore.open(db, hwm)
        store.commit_round([self.record("1,1", 1)], budget_ns=1000, degraded=False)
        store.close()
        shutil.copyfile(backup, db)
        with self.assertRaises(AuditError) as ctx:
            LedgerStore.open(db, hwm)
        self.assertEqual(ctx.exception.code, ErrorCode.SNAPSHOT_ROLLBACK)

    def test_checksum_corruption_detection(self) -> None:
        ledger, sources = self._ledger_and_sources()
        db = self.root / "ledger.sqlite3"
        hwm = self.root / "hwm.json"
        store = LedgerStore.create(db, ledger_version=1, adjudicated=ledger, source_checksums=sources, high_water_path=hwm)
        store.commit_round([self.record("0,0", 0)], budget_ns=1000, degraded=False)
        store.close()
        conn = sqlite3.connect(db)
        conn.execute("UPDATE probe_ledger SET checksum='sha256:bad' WHERE uid='0,0'")
        conn.commit()
        conn.close()
        with self.assertRaises(AuditError) as ctx:
            LedgerStore.open(db, hwm)
        self.assertEqual(ctx.exception.code, ErrorCode.SNAPSHOT_CORRUPT)

    def test_garbage_snapshot_is_corrupt(self) -> None:
        db = self.root / "garbage.sqlite3"
        hwm = self.root / "garbage.hwm.json"
        db.write_bytes(b"not a sqlite database")
        with self.assertRaises(AuditError) as ctx:
            LedgerStore.open(db, hwm)
        self.assertEqual(ctx.exception.code, ErrorCode.SNAPSHOT_CORRUPT)


class PipelineTests(SampleTestBase):
    def test_end_to_end_audit_and_replay_determinism(self) -> None:
        config_path = self.write_config()
        report = run_audit(config_path, fresh=True)
        self.assertEqual(report["self_check"]["status"], "PASS")
        self.assertEqual(len(report["probes"]), 10)
        self.assertEqual(report["plan_summary"]["rejected_count"], 2)
        self.assertEqual(report["plan_summary"]["suppressed_count"], 1)
        self.assertEqual(report["ledger"]["ledger_revision"], 5)
        self.assertEqual(report["conflict_summary"]["first_conflict"], {"x": 15, "y": 1})

        degraded = [p for p in report["probes"] if p["degradation"]["degraded"]]
        self.assertEqual([p["probe_id"] for p in degraded], ["P-LOW-3", "P-LOW-4"])
        self.assertTrue(all("raw_truth" in p and set(p["raw_truth"]) == {"v1", "v2", "RLE"} for p in report["probes"]))
        self.assertTrue(all(isinstance(p["timing"]["elapsed_ns"], int) and p["timing"]["elapsed_ns"] >= 0 for p in report["probes"]))

        audit_path = Path(json.loads(config_path.read_text())["audit_output_path"])
        first_bytes = audit_path.read_bytes()
        regenerated = regenerate_audit(config_path)
        second_bytes = audit_path.read_bytes()
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(report["report_checksum"], regenerated["report_checksum"])

        rerun = run_audit(config_path)
        self.assertEqual(rerun["ledger"]["ledger_revision"], 5)
        self.assertEqual(rerun["report_checksum"], regenerated["report_checksum"])
        self.assertEqual(audit_path.read_bytes(), second_bytes)

    def test_self_check_reports_first_bad_probe(self) -> None:
        config_path = self.write_config()
        report = run_audit(config_path, fresh=True)
        db = json.loads(config_path.read_text())["ledger_path"]
        hwm = json.loads(config_path.read_text())["high_water_path"]
        store = LedgerStore.open(db, hwm)
        bad_report = json.loads(json.dumps(report))
        bad_report["probes"][3]["record_checksum"] = "sha256:wrong"
        bad_report["report_checksum"] = "sha256:recalculated-not-relevant"
        with self.assertRaises(AuditError) as ctx:
            cross_module_check(
                sample_dir=self.sample_dir,
                manifest=self.manifest,
                format_priority=["RLE", "v2", "v1"],
                accepted=[],
                store=store,
                report=bad_report,
            )
        store.close()
        self.assertEqual(ctx.exception.code, ErrorCode.INCONSISTENT)
        self.assertIn("first_deviation_probe_id", ctx.exception.details)

    def test_degradation_policy_uses_slow_budget_share(self) -> None:
        records = [
            {"timing": {"charged_ns": 10}, "execution": {"speed_class": "fast"}},
            {"timing": {"charged_ns": 900}, "execution": {"speed_class": "slow"}},
        ]
        decision = evaluate_degradation(records, total_budget_ns=10_000, threshold=0.35)
        self.assertTrue(decision["triggered"])
        self.assertGreater(decision["slow_cost_ratio"], 0.35)


if __name__ == "__main__":
    unittest.main()
