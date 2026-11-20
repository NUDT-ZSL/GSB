from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from embedding_ops.cli import sample_specs
from embedding_ops.config import NumericsConfig
from embedding_ops.errors import BusinessError
from embedding_ops.manifest import (
    load_manifest,
    load_manifest_with_duplicates,
    write_manifest,
)
from embedding_ops.reporting import build_report
from embedding_ops.snapshots import (
    SnapshotStore,
    create_snapshot,
    snapshot_from_mapping,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SnapshotTests(unittest.TestCase):
    def test_whole_replacement_and_removed_old_keys(self) -> None:
        store = SnapshotStore()
        first = create_snapshot(
            "config",
            1,
            {"label": "v1", "legacy_only": True, "nested": {"old": 1}},
        )
        second = create_snapshot(
            "config",
            2,
            {"label": "v2", "nested": {"new": 2}},
        )
        self.assertEqual(store.load(first).action, "loaded")
        outcome = store.load(second)
        self.assertEqual(outcome.action, "replaced")
        current = store.current("config")
        assert current is not None
        self.assertEqual(current.version, 2)
        self.assertNotIn("legacy_only", current.content)
        self.assertEqual(current.content["nested"], {"new": 2})

    def test_rollback_and_corruption(self) -> None:
        store = SnapshotStore()
        first = create_snapshot("quant_params", 1, {"qint8": 1})
        second = create_snapshot("quant_params", 2, {"qint8": 2})
        store.load(first)
        store.load(second)
        with self.assertRaises(BusinessError) as rollback:
            store.load(first)
        self.assertEqual(rollback.exception.code, "E_SNAPSHOT_ROLLBACK")
        corrupt = second.to_dict()
        corrupt["content"]["qint8"] = 99
        with self.assertRaises(BusinessError) as checksum:
            store.load(corrupt)
        self.assertEqual(checksum.exception.code, "E_SNAPSHOT_CORRUPT")
        current = store.current("quant_params")
        assert current is not None
        self.assertEqual(current.version, 2)
        self.assertEqual(current.content["qint8"], 2)

    def test_same_version_with_different_checksum_is_corrupt(self) -> None:
        store = SnapshotStore()
        store.load(create_snapshot("spectrum_stats", 1, {"value": 1}))
        changed = create_snapshot("spectrum_stats", 1, {"value": 2})
        with self.assertRaises(BusinessError) as caught:
            store.load(changed)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_mapping_round_trip(self) -> None:
        snapshot = create_snapshot("config", 3, {"a": 1})
        parsed = snapshot_from_mapping(snapshot.to_dict())
        self.assertEqual(parsed, snapshot)


class ManifestTests(unittest.TestCase):
    def test_duplicate_job_is_structured(self) -> None:
        specs = sample_specs()[:2]
        specs[1] = replace(specs[1], job_id=specs[0].job_id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.jsonl"
            write_manifest(path, specs)
            with self.assertRaises(BusinessError) as caught:
                load_manifest(path)
            self.assertEqual(caught.exception.code, "E_DUP_JOB")
            unique, duplicates = load_manifest_with_duplicates(path)
            self.assertEqual(len(unique), 1)
            self.assertEqual(duplicates[0]["code"], "E_DUP_JOB")
            self.assertEqual(duplicates[0]["job_id"], specs[0].job_id)


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = build_report(sample_specs(), NumericsConfig())

    def test_report_contains_required_sections(self) -> None:
        self.assertIn("jobs", self.report)
        self.assertIn("precision_switches", self.report)
        self.assertIn("injections", self.report)
        self.assertIn("snapshots", self.report)
        self.assertIn("unattributed_exceptions", self.report)
        summary = self.report["summary"]
        self.assertEqual(summary["unattributed_exceptions"], 0)
        self.assertEqual(
            summary["self_checks_passed"], summary["self_checks_total"]
        )
        self.assertEqual(
            summary["injections_raised"], summary["injections_total"]
        )
        self.assertEqual(self.report["snapshots"]["replacement_checks"],
                         {
                             "config_legacy_key_removed": True,
                             "config_whole_content_replaced": True,
                             "corruption_preserved_version": True,
                             "quant_legacy_key_removed": True,
                             "rollback_preserved_version": True,
                         })

    def test_snapshot_errors_are_in_business_codes(self) -> None:
        codes = self.report["summary"]["business_codes"]
        self.assertIn("E_SNAPSHOT_ROLLBACK", codes)
        self.assertIn("E_SNAPSHOT_CORRUPT", codes)


class CliReproducibilityTests(unittest.TestCase):
    def test_cli_report_is_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.json"
            second = Path(directory) / "second.json"
            command = [
                sys.executable,
                "run_demo.py",
                "--jobs",
                str(PROJECT_ROOT / "samples" / "tasks.jsonl"),
                "--compact",
            ]
            for output in (first, second):
                completed = subprocess.run(
                    command + ["--output", str(output)],
                    cwd=PROJECT_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            report = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["unattributed_exceptions"], 0)
            self.assertEqual(
                report["summary"]["self_checks_passed"],
                report["summary"]["self_checks_total"],
            )


if __name__ == "__main__":
    unittest.main()
