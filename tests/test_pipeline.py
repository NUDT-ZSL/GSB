import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread

from risk_governance.boundary import diagnose_boundaries
from risk_governance.calibration import calibrate_batch
from risk_governance.canonical import canonical_dumps, read_json, sha256_json
from risk_governance.errors import (
    E_BOUNDARY_FLIP,
    E_BOUNDARY_STATS,
    E_QUANTILE_ORDER,
    E_SNAPSHOT_ROLLBACK,
    E_ZERO_DENOM,
)
from risk_governance.events import coverage_check, resolve_event_closure
from risk_governance.generator import generate_files
from risk_governance.pipeline import GovernancePipeline
from risk_governance.snapshots import SnapshotManager


class PipelineAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="risk-governance-test-")
        cls.root = Path(cls.temp.name)
        cls.data = generate_files(cls.root / "data", seed=416)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def _pipeline(self, workdir, *, baseline=False, events=True, interrupt=None, delay=0):
        config = self.data["baseline_config"] if baseline else self.data["config"]
        events_path = self.data["events"] if events else None
        return GovernancePipeline(
            workdir=self.root / workdir,
            samples_path=self.data["samples"],
            config_path=config,
            events_path=events_path,
            now_ms=1764000000000,
            interrupt_after_batch=interrupt,
            batch_delay_ms=delay,
            seed=416,
        )

    def _assert_report_checksum(self, report):
        checksum = report["checksum"]
        body = {key: value for key, value in report.items() if key != "checksum"}
        self.assertEqual(checksum, sha256_json(body))

    def test_generator_is_deterministic(self):
        first = generate_files(self.root / "gen-a", seed=416)
        second = generate_files(self.root / "gen-b", seed=416)
        for name in ("samples", "config", "baseline_config", "events"):
            self.assertEqual(
                sha256_json(read_json(first[name])) if first[name].suffix == ".json" else self._file_hash(first[name]),
                sha256_json(read_json(second[name])) if second[name].suffix == ".json" else self._file_hash(second[name]),
            )

    @staticmethod
    def _file_hash(path):
        from risk_governance.canonical import sha256_file

        return sha256_file(path)

    def test_targeted_recompute_and_coverage(self):
        baseline_dir = self.root / "target-baseline"
        code, baseline_report = self._pipeline("target-baseline", baseline=True, events=False).execute()
        self.assertEqual(code, 0)
        self.assertEqual(len(baseline_report["planned_recompute_set"]), 9)

        code, report = self._pipeline("target-baseline").execute()
        self.assertEqual(code, 0)
        self.assertEqual(report["planned_recompute_set"], ["B002", "B003", "B004", "B006"])
        self.assertEqual(report["reused_set"], ["B000", "B001", "B005", "B007", "B008"])
        self.assertTrue(report["coverage_check"]["full_coverage"])
        self.assertEqual(report["event_closure_check"]["event_closure_without_recompute"], [])
        self.assertEqual(report["event_closure_check"]["recompute_without_event_closure"], [])
        self._assert_report_checksum(report)
        self.assertTrue(report["cache_reference_chain"]["valid"])

    def test_interrupt_recovery_skips_committed_and_clears_pending(self):
        workdir = self.root / "recovery"
        code, interrupted = self._pipeline("recovery", interrupt=2).execute()
        self.assertEqual(code, 143)
        self.assertEqual(interrupted["status"], "interrupted")
        state = read_json(workdir / "state" / "recovery.json")
        self.assertEqual(state["committed_batches"], ["B000", "B001"])
        self.assertEqual(state["committed_stage_index"], 2)
        self.assertTrue((workdir / "state" / "inflight" / "B002.json").is_file())
        ledger_file = workdir / "ledger" / "batches" / "B000.json"
        inode_before = ledger_file.stat().st_ino

        code, report = self._pipeline("recovery").execute()
        self.assertEqual(code, 0)
        self.assertTrue(report["recovery"]["recovered"])
        self.assertEqual(report["recovered_skipped_batch_ids"], ["B000", "B001"])
        self.assertEqual(report["reused_set"], ["B000", "B001"])
        self.assertNotIn("B000", report["computed_batch_ids"])
        self.assertNotIn("B001", report["computed_batch_ids"])
        self.assertIn("B002", report["computed_batch_ids"])
        self.assertTrue(report["coverage_check"]["full_coverage"])
        self.assertEqual(ledger_file.stat().st_ino, inode_before)
        self.assertEqual(list((workdir / "state" / "inflight").glob("*")), [])
        self.assertEqual(list((workdir / "cache" / "pending").glob("*")), [])
        cleaned_kinds = {item["kind"] for item in report["recovery"]["cleaned"]}
        self.assertIn("inflight_marker", cleaned_kinds)
        self.assertIn("pending_cache_pointer", cleaned_kinds)

    def test_warm_reports_are_byte_identical(self):
        pipeline = self._pipeline("determinism")
        self.assertEqual(pipeline.execute()[0], 0)
        self.assertEqual(self._pipeline("determinism").execute()[0], 0)
        report_path = self.root / "determinism" / "reports" / "execution_report.json"
        first_bytes = report_path.read_bytes()
        self.assertEqual(self._pipeline("determinism").execute()[0], 0)
        second_bytes = report_path.read_bytes()
        self.assertEqual(first_bytes, second_bytes)

    def test_quantile_error_codes(self):
        base_stats = {
            "batch_id": "B-X",
            "cycle_id": "C9",
            "score": {
                "dtype": "numeric",
                "quantiles": [
                    {"probability": 0.1, "value": 0.2},
                    {"probability": 0.5, "value": 0.1},
                    {"probability": 0.9, "value": 0.8},
                ],
            },
        }
        config = {
            "calibrations": [
                {
                    "item": "score",
                    "anchor_probabilities": [0.1, 0.5, 0.9],
                    "target_anchor_values": [0.1, 0.5, 0.9],
                    "segments": [
                        {"segment_index": 0, "left_probability": 0.1, "right_probability": 0.5},
                        {"segment_index": 1, "left_probability": 0.5, "right_probability": 0.9},
                    ],
                }
            ]
        }
        _, errors = calibrate_batch(base_stats, config)
        self.assertTrue(any(e["code"] == E_QUANTILE_ORDER and e["segment_index"] == 0 for e in errors))

        zero_stats = json.loads(canonical_dumps(base_stats))
        zero_stats["score"]["quantiles"][1]["value"] = 0.2
        _, errors = calibrate_batch(zero_stats, config)
        self.assertTrue(any(e["code"] == E_ZERO_DENOM and e["segment_index"] == 0 for e in errors))

        missing_stats = json.loads(canonical_dumps(base_stats))
        missing_stats["score"]["quantiles"] = [missing_stats["score"]["quantiles"][0], missing_stats["score"]["quantiles"][2]]
        _, errors = calibrate_batch(missing_stats, config)
        self.assertTrue(any(e["code"] == E_QUANTILE_ORDER and e.get("reason") == "missing_quantile" for e in errors))

    def test_boundary_stats_and_flip_codes(self):
        target = {
            "target": "unit",
            "threshold": 0.5,
            "max_flips": 0,
            "hysteresis_margin": 0.01,
        }
        _, errors = diagnose_boundaries(
            [{"batch_id": "EMPTY", "cycle_id": "C0", "records": []}],
            {"boundary_targets": [target]},
        )
        self.assertTrue(any(e["code"] == E_BOUNDARY_STATS for e in errors))

        datasets = [
            {
                "batch_id": "B0",
                "cycle_id": "C0",
                "records": [
                    {"_line_no": 1, "entity_id": "E1", "score": 0.1, "label": 0},
                    {"_line_no": 2, "entity_id": "E2", "score": 0.9, "label": 1},
                ],
            },
            {
                "batch_id": "B1",
                "cycle_id": "C1",
                "records": [
                    {"_line_no": 1, "entity_id": "E1", "score": 0.9, "label": 1},
                    {"_line_no": 2, "entity_id": "E2", "score": 0.1, "label": 0},
                ],
            },
        ]
        diagnoses, errors = diagnose_boundaries(datasets, {"boundary_targets": [target]})
        self.assertEqual(diagnoses[0]["flip_count"], 2)
        self.assertTrue(any(e["code"] == E_BOUNDARY_FLIP and len(e["replay_evidence"]) == 2 for e in errors))

    def test_event_closure_and_coverage(self):
        batches = ["B0", "B1", "B2"]
        cycles = {"B0": "C0", "B1": "C0", "B2": "C1"}
        event = {
            "event_seq": 9,
            "batch_id": "B1",
            "change_dimension": "feature_redefinition.region",
            "new_config_summary": {"affected_batch_ids": ["B1", "B2"]},
        }
        self.assertEqual(resolve_event_closure(event, batches, cycles, {}), ["B1", "B2"])
        check = coverage_check(batches, ["B1"], ["B0", "B2"])
        self.assertTrue(check["full_coverage"])
        bad = coverage_check(batches, ["B1"], ["B1"])
        self.assertFalse(bad["full_coverage"])
        self.assertEqual(bad["missing_batch_ids"], ["B0", "B2"])

    def test_snapshot_rollback_and_concurrent_reads(self):
        manager = SnapshotManager(self.root / "snapshots-concurrency")
        manager.publish_v1({"v": 0}, 1764000000000)
        manager.publish_v2({"v": 0}, 1764000000000)
        rollback = manager.attempt_rollback(0)
        self.assertFalse(rollback["allowed"])
        self.assertEqual(rollback["error"]["code"], E_SNAPSHOT_ROLLBACK)

        errors = []
        stop = False

        def reader():
            while not stop:
                try:
                    result = manager.verify_current_v2()
                    if not result["valid"]:
                        errors.append(result)
                except FileNotFoundError:
                    pass
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(repr(exc))

        thread = Thread(target=reader)
        thread.start()
        try:
            for version in range(1, 12):
                manager.publish_v2(
                    {"v": version, "batch_summaries": [], "errors": []},
                    1764000000000 + version,
                )
        finally:
            stop = True
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(manager.current_v2_manifest()["version"], 12)

    def test_external_sigterm_persists_recovery(self):
        workdir = self.root / "sigterm"
        cmd = [
            sys.executable,
            "-m",
            "risk_governance.cli",
            "run",
            "--samples",
            str(self.data["samples"]),
            "--config",
            str(self.data["config"]),
            "--events",
            str(self.data["events"]),
            "--workdir",
            str(workdir),
            "--now-ms",
            "1764000000000",
            "--batch-delay-ms",
            "500",
        ]
        process = subprocess.Popen(
            cmd,
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.2)
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        return_code = process.returncode
        self.assertEqual(return_code, 143)
        state = read_json(workdir / "state" / "recovery.json")
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["signal"], 15)
        self.assertIn("persisted stage", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
