"""Subprocess-level tests for signals, atomic writes and invalid samples."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
CLI = [PYTHON, "-m", "pipeline.cli"]

CONFIG = os.path.join(ROOT, "configs", "fusion_config.json")
RELATIONS = os.path.join(ROOT, "configs", "relations.json")
INJECTIONS = os.path.join(ROOT, "configs", "injections.json")
EMPTY = os.path.join(ROOT, "configs", "injections_empty.json")


def run_cli(args, env=None, signal_after=None):
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    proc = subprocess.Popen(
        CLI + args, cwd=ROOT, env=merged_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if signal_after is not None:
        time.sleep(signal_after)
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    return proc.returncode, out, err


class IntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pipeline-it-")
        cls.input_dir = os.path.join(cls.tmp, "samples")
        cls.manifest = os.path.join(cls.tmp, "manifest.json")
        run_cli(["generate", "--input-dir", cls.input_dir,
                 "--config", CONFIG, "--samples", "8",
                 "--manifest", cls.manifest])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_manifest_ordering(self):
        doc = json.load(open(self.manifest))
        entries = [(e["sample"], e["round"]) for e in doc["entries"]]
        self.assertEqual(entries, sorted(entries))
        self.assertEqual(doc["ordering"], "sample_name_then_round")

    def test_sigterm_graceful_exit_then_resume(self):
        # A larger tree keeps the engine alive long enough for the signal.
        big_input = os.path.join(self.tmp, "sigterm-samples")
        big_manifest = os.path.join(self.tmp, "sigterm-manifest.json")
        run_cli(["generate", "--input-dir", big_input, "--config", CONFIG,
                 "--samples", "32", "--manifest", big_manifest])
        work = os.path.join(self.tmp, "sigterm-work")
        report = os.path.join(self.tmp, "sigterm-report.json")
        code, _out, err = run_cli(
            ["run", "--input-dir", big_input, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", INJECTIONS,
             "--work-dir", work, "--report", report],
            signal_after=0.4)
        self.assertEqual(code, 0)
        self.assertIn("graceful exit", err)
        self.assertFalse(os.path.exists(report))
        snapshot = json.load(open(os.path.join(work, "snapshot.json")))
        self.assertGreaterEqual(snapshot["round"], 0)
        self.assertTrue(os.path.exists(os.path.join(work, "failures.json")))

        code, _out, _err = run_cli(
            ["run", "--input-dir", big_input, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", INJECTIONS,
             "--work-dir", work, "--report", report])
        self.assertEqual(code, 0)
        full_report = os.path.join(self.tmp, "full.json")
        code, _out, _err = run_cli(
            ["run", "--input-dir", big_input, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", INJECTIONS,
             "--work-dir", os.path.join(self.tmp, "full-work"),
             "--report", full_report])
        self.assertEqual(code, 0)
        with open(report, "rb") as first, open(full_report, "rb") as second:
            self.assertEqual(first.read(), second.read())

    def test_sigkill_leaves_consistent_snapshot_only(self):
        work = os.path.join(self.tmp, "kill-work")
        report = os.path.join(self.tmp, "kill-report.json")
        env = {"PIPELINE_INTERRUPT_AFTER_ROUND": "0"}
        code, _out, _err = run_cli(
            ["run", "--input-dir", self.input_dir, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", EMPTY,
             "--work-dir", work, "--report", report], env=env)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(report))
        snapshot_path = os.path.join(work, "snapshot.json")
        snapshot = json.load(open(snapshot_path))
        self.assertEqual(snapshot["round"], 0)
        # Resume must reconstruct the identical final report.
        resumed_report = os.path.join(self.tmp, "kill-resume.json")
        run_cli(["run", "--input-dir", self.input_dir, "--config", CONFIG,
                 "--relations", RELATIONS, "--injections", EMPTY,
                 "--work-dir", work, "--report", resumed_report])
        full_report = os.path.join(self.tmp, "kill-full.json")
        run_cli(["run", "--input-dir", self.input_dir, "--config", CONFIG,
                 "--relations", RELATIONS, "--injections", EMPTY,
                 "--work-dir", os.path.join(self.tmp, "kill-full-work"),
                 "--report", full_report])
        with open(resumed_report, "rb") as first, \
                open(full_report, "rb") as second:
            self.assertEqual(first.read(), second.read())

    def test_invalid_sample_recorded_not_skipped(self):
        broken_input = os.path.join(self.tmp, "broken-samples")
        shutil.copytree(self.input_dir, broken_input)
        target = os.path.join(broken_input, "sample-002", "image.png")
        with open(target, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        work = os.path.join(self.tmp, "broken-work")
        report = os.path.join(self.tmp, "broken-report.json")
        code, _out, _err = run_cli(
            ["run", "--input-dir", broken_input, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", EMPTY,
             "--work-dir", work, "--report", report])
        self.assertEqual(code, 0)
        doc = json.load(open(report))
        invalid = [f for f in doc["failures"]
                   if f["sample"] == "sample-002"
                   and f["code"] == "E_SAMPLE_INVALID"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(doc["statistics"]["samples_total"], 8)
        self.assertEqual(doc["statistics"]["samples_completed"], 7)

    def test_report_written_atomically_and_canonically(self):
        report = os.path.join(self.tmp, "atomic.json")
        work = os.path.join(self.tmp, "atomic-work")
        code, _out, _err = run_cli(
            ["run", "--input-dir", self.input_dir, "--config", CONFIG,
             "--relations", RELATIONS, "--injections", EMPTY,
             "--work-dir", work, "--report", report])
        self.assertEqual(code, 0)
        raw = open(report, "rb").read()
        doc = json.loads(raw)
        reencoded = json.dumps(
            doc, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")
        self.assertEqual(raw, reencoded)
        leftovers = [name for name in os.listdir(os.path.dirname(report))
                     if name.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
