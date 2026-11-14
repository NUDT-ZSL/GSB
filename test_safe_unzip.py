#!/usr/bin/env python3
"""test_safe_unzip.py - Acceptance tests for safe_unzip.py (stdlib unittest).

Coverage map (requirement number -> test method):
  1 path containment / traversal / absolute rules
      test_classify_entry_rules, test_malicious_zip_rejected
  2 unsafe archive => whole refusal, empty target, visible error
      test_malicious_zip_rejected
  3 safe archive => structure extracted, full JSON report
      test_safe_zip_extraction_and_report
  4 every unsafe entry listed, archive order kept, safe entries visible
      test_malicious_zip_rejected
  5 corrupt archive => clear error, no target, no misleading report
      test_corrupt_zip
  6 write failure => rollback to pre-operation state + failure report
      test_restricted_directory_write_failure
      test_mid_extraction_failure_rolls_back
  7 report JSON schema (name/status/reason types and values)
      test_report_schema
  8 CLI args: usage + non-zero exit on missing/invalid args
      test_cli_arguments
  9 ZIP timestamps preserved; timestamp failure tolerated
      test_timestamps_preserved, test_timestamp_failure_tolerated
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

# Make the sibling module importable even under "python -I".
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safe_unzip  # noqa: E402
import make_samples  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "safe_unzip.py")
FIXED_DT = datetime(2025, 1, 15, 8, 30, 45, tzinfo=timezone.utc)


def run_cli(*cli_args: str) -> subprocess.CompletedProcess:
    """Run the tool as a real CLI process (isolated mode, untrusted input)."""
    return subprocess.run(
        [sys.executable, "-I", SCRIPT, *cli_args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class SafeUnzipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._workspace_ctx = tempfile.TemporaryDirectory(prefix="safe_unzip_t_")
        cls.workspace = cls._workspace_ctx.name
        cls.samples = os.path.join(cls.workspace, "samples")
        os.makedirs(cls.samples, exist_ok=True)
        make_samples.make_safe_zip(os.path.join(cls.samples, "safe.zip"))
        make_samples.make_malicious_zip(os.path.join(cls.samples, "malicious.zip"))
        make_samples.make_corrupt_zip(os.path.join(cls.samples, "corrupt.zip"))
        cls.safe_zip = os.path.join(cls.samples, "safe.zip")
        cls.malicious_zip = os.path.join(cls.samples, "malicious.zip")
        cls.corrupt_zip = os.path.join(cls.samples, "corrupt.zip")
        cls._seq = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls._workspace_ctx.cleanup()

    def _new_target(self) -> str:
        # Unique, guaranteed non-existent target path across all test methods.
        SafeUnzipTests._seq += 1
        return os.path.join(self.workspace, f"target_{SafeUnzipTests._seq:03d}")

    def setUp(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # Requirement 1: per-entry containment rules (pure unit level)
    # ------------------------------------------------------------------ #
    def test_classify_entry_rules(self) -> None:
        target = os.path.join(self.workspace, "classify_target")
        cases = [
            ("readme.txt", "safe"),
            ("docs/intro.md", "safe"),
            ("a/b/c/../d.txt", "unsafe"),       # '..' component
            ("../escape.txt", "unsafe"),
            ("../../etc/passwd", "unsafe"),
            ("..\\windows_escape.txt", "unsafe"),
            ("a\\..\\..\\evil", "unsafe"),
            ("/etc/passwd", "unsafe"),
            ("//server/share/x", "unsafe"),
            ("C:/windows/x.txt", "unsafe"),
            ("C:evil.txt", "unsafe"),
        ]
        for name, expected_status in cases:
            with self.subTest(entry=name):
                result = safe_unzip.classify_entry(name, target)
                self.assertEqual(result.status, expected_status)
                self.assertEqual(result.name, name)
                if expected_status == "unsafe":
                    self.assertTrue(result.reason)
                else:
                    self.assertEqual(result.reason, "")

    # ------------------------------------------------------------------ #
    # Requirements 2 + 4: refusal, no writes, full ordered unsafe listing
    # ------------------------------------------------------------------ #
    def test_malicious_zip_rejected(self) -> None:
        # Case A: target does not exist yet -> must never be created.
        target = self._new_target()
        report = os.path.join(self.workspace, "report_a.json")
        proc = run_cli(self.malicious_zip, target, report)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.returncode, safe_unzip.EXIT_UNSAFE)
        self.assertIn("refused", proc.stderr.lower())
        self.assertIn("unsafe", proc.stderr.lower())
        self.assertFalse(os.path.exists(target), "target must not be created")
        self.assertFalse(os.path.exists(report), "no report on wholesale refusal")

        # Case B: pre-existing empty target -> must remain empty.
        target_b = self._new_target()
        os.makedirs(target_b)
        proc = run_cli(self.malicious_zip, target_b)
        self.assertEqual(proc.returncode, safe_unzip.EXIT_UNSAFE)
        self.assertEqual(os.listdir(target_b), [])

        # In-process inspection of every verdict, in archive order.
        results = [
            safe_unzip.classify_entry(name, target_b)
            for name, _ in make_samples.MALICIOUS_ENTRIES
        ]
        expected = [
            ("good/start.txt", "safe"),
            ("../escape.txt", "unsafe"),
            ("/tmp/absolute_posix.txt", "unsafe"),
            ("good/middle.txt", "safe"),
            ("..\\windows_escape.txt", "unsafe"),
            ("C:/windows_drive.txt", "unsafe"),
            ("good/deep/end.txt", "safe"),
        ]
        self.assertEqual(len(results), len(expected))
        unsafe_names = []
        for result, (name, status) in zip(results, expected):
            self.assertEqual(result.name, name)
            self.assertEqual(result.status, status)
            if status == "unsafe":
                unsafe_names.append(name)
                self.assertTrue(result.reason)
        # All four unsafe entries reported, archive order preserved.
        self.assertEqual(
            unsafe_names,
            [
                "../escape.txt",
                "/tmp/absolute_posix.txt",
                "..\\windows_escape.txt",
                "C:/windows_drive.txt",
            ],
        )
        # Safe entries remain visible one-by-one.
        self.assertEqual(
            [r.name for r in results if r.status == "safe"],
            ["good/start.txt", "good/middle.txt", "good/deep/end.txt"],
        )

    # ------------------------------------------------------------------ #
    # Requirements 3 + 7 + 9: safe extraction, structure, report, mtimes
    # ------------------------------------------------------------------ #
    def test_safe_zip_extraction_and_report(self) -> None:
        target = self._new_target()
        report = os.path.join(self.workspace, "report_safe.json")
        proc = run_cli(self.safe_zip, target, report)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # Archive structure preserved.
        for rel, payload in make_samples.SAFE_ENTRIES:
            destination = os.path.join(target, *rel.split("/"))
            if payload is None:
                self.assertTrue(os.path.isdir(destination), rel)
            else:
                self.assertTrue(os.path.isfile(destination), rel)
                with open(destination, "rb") as fh:
                    self.assertEqual(fh.read(), payload)

        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["result"], "extracted")
        self.assertEqual(data["unsafe_count"], 0)
        self.assertEqual(data["entry_count"], len(make_samples.SAFE_ENTRIES))
        self.assertIsNone(data["error"])
        self.assertEqual(
            [e["name"] for e in data["entries"]],
            [name for name, _ in make_samples.SAFE_ENTRIES],
        )
        for entry in data["entries"]:
            self.assertEqual(entry["status"], "safe")
            self.assertIsInstance(entry["reason"], str)

    def test_report_schema(self) -> None:
        target = self._new_target()
        report = os.path.join(self.workspace, "report_schema.json")
        proc = run_cli(self.safe_zip, target, report)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        for entry in data["entries"]:
            self.assertIsInstance(entry["name"], str)
            self.assertTrue(entry["name"])
            self.assertIn(entry["status"], ("safe", "unsafe"))
            self.assertIsInstance(entry["reason"], str)

    def test_timestamps_preserved(self) -> None:
        target = self._new_target()
        report = os.path.join(self.workspace, "report_mtime.json")
        proc = run_cli(self.safe_zip, target, report)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected_epoch = FIXED_DT.timestamp()
        checked = 0
        for rel, payload in make_samples.SAFE_ENTRIES:
            destination = os.path.join(target, *rel.split("/"))
            mtime = os.stat(destination).st_mtime
            self.assertAlmostEqual(mtime, expected_epoch, delta=1.0)
            checked += 1
        self.assertEqual(checked, len(make_samples.SAFE_ENTRIES))

    def test_timestamp_failure_tolerated(self) -> None:
        target = self._new_target()
        report = os.path.join(self.workspace, "report_mtime_fail.json")
        with mock.patch("os.utime", side_effect=OSError("simulated utime error")):
            result = safe_unzip.safe_extract(self.safe_zip, target, report)
        # Extraction still complete ...
        self.assertTrue(os.path.isfile(os.path.join(target, "readme.txt")))
        self.assertTrue(os.path.isdir(os.path.join(target, "empty_dir")))
        # ... and the report is still produced.
        self.assertTrue(os.path.isfile(report))
        self.assertEqual(result["result"], "extracted")
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(
            [e["status"] for e in data["entries"]],
            ["safe"] * len(make_samples.SAFE_ENTRIES),
        )

    # ------------------------------------------------------------------ #
    # Requirement 5: corrupt archive
    # ------------------------------------------------------------------ #
    def test_corrupt_zip(self) -> None:
        target = self._new_target()
        report = os.path.join(self.workspace, "report_corrupt.json")
        proc = run_cli(self.corrupt_zip, target, report)
        self.assertEqual(proc.returncode, safe_unzip.EXIT_CORRUPT)
        self.assertTrue(proc.stderr.strip())
        self.assertIn("corrupt", proc.stderr.lower())
        self.assertFalse(os.path.exists(target), "target must not be created")
        self.assertFalse(os.path.exists(report), "no misleading report on corruption")

    # ------------------------------------------------------------------ #
    # Requirement 6: write failures and rollback
    # ------------------------------------------------------------------ #
    def test_mid_extraction_failure_rolls_back(self) -> None:
        """Simulated failure on the second file: pre-existing state kept."""
        target = self._new_target()
        os.makedirs(target)
        keep_file = os.path.join(target, "pre_existing.txt")
        with open(keep_file, "wb") as fh:
            fh.write(b"must survive\n")
        report = os.path.join(self.workspace, "report_midfail.json")

        real_open = safe_unzip._open
        wb_calls: list[str] = []

        def fake_open(path, mode="r", *args, **kwargs):
            if mode == "wb":
                wb_calls.append(path)
                if len(wb_calls) == 2:
                    raise PermissionError(f"simulated failure writing {path}")
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(safe_unzip, "_open", fake_open):
            with self.assertRaises(safe_unzip.ExtractionFailedError) as ctx:
                safe_unzip.safe_extract(self.safe_zip, target, report)
        self.assertIn("rolled back", str(ctx.exception))

        # Only the pre-existing file remains; nothing extracted is left.
        self.assertEqual(os.listdir(target), ["pre_existing.txt"])
        with open(keep_file, "rb") as fh:
            self.assertEqual(fh.read(), b"must survive\n")
        # A failure report explaining the reason is still written.
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["result"], "failed")
        self.assertTrue(data["error"])
        self.assertIn("rolled back", data["error"])

    def test_restricted_directory_write_failure(self) -> None:
        """Real OS-level restriction: creating files is denied."""
        target = self._new_target()
        os.makedirs(target)
        keep = os.path.join(target, "keep.txt")
        with open(keep, "wb") as fh:
            fh.write(b"original\n")
        report = os.path.join(self.workspace, "report_restricted.json")

        restricted = False
        try:
            if os.name == "nt":
                user = os.environ.get("USERNAME") or os.environ.get("USER")
                cmds = [
                    ["icacls", target, "/inheritance:r"],
                    ["icacls", target, "/grant:r", f"{user}:(RX)"],
                    ["icacls", target, "/deny", f"{user}:(WD,AD,WA)"],
                ]
                for cmd in cmds:
                    completed = subprocess.run(cmd, capture_output=True, text=True)
                    if completed.returncode != 0:
                        self.skipTest(f"icacls restriction unavailable: {cmd}")
                # Probe that the restriction really blocks file creation.
                probe = os.path.join(target, "probe.txt")
                try:
                    with open(probe, "wb") as fh:
                        fh.write(b"x")
                    os.remove(probe)
                    self.skipTest("write restriction not effective for this user")
                except PermissionError:
                    restricted = True
            else:
                if getattr(os, "geteuid", lambda: 1)() == 0:
                    self.skipTest("root bypasses POSIX permission bits")
                os.chmod(target, 0o500)
                probe = os.path.join(target, "probe.txt")
                try:
                    with open(probe, "wb") as fh:
                        fh.write(b"x")
                    os.remove(probe)
                    self.skipTest("write restriction not effective")
                except PermissionError:
                    restricted = True

            proc = run_cli(self.safe_zip, target, report)
            self.assertEqual(
                proc.returncode,
                safe_unzip.EXIT_EXTRACT_FAILED,
                proc.stderr,
            )
            self.assertIn("rolled back", proc.stderr.lower())
            # No residue from the failed attempt; pre-existing file still
            # listed (its content is checked after permissions are restored).
            self.assertEqual(os.listdir(target), ["keep.txt"])
            with open(report, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["result"], "failed")
            self.assertTrue(data["error"])
        finally:
            if restricted:
                if os.name == "nt":
                    subprocess.run(
                        ["icacls", target, "/reset", "/T", "/C"],
                        capture_output=True,
                    )
                else:
                    os.chmod(target, 0o700)

        # Pre-existing file content untouched (check post-restoration).
        with open(keep, "rb") as fh:
            self.assertEqual(fh.read(), b"original\n")

    # ------------------------------------------------------------------ #
    # Requirement 8: CLI argument handling
    # ------------------------------------------------------------------ #
    def test_cli_arguments(self) -> None:
        # Missing both arguments -> usage text, non-zero exit.
        proc = run_cli()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stderr.lower())

        # Missing one argument -> usage text, non-zero exit.
        proc = run_cli(self.safe_zip)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("usage", proc.stderr.lower())

        # Invalid ZIP path -> non-zero exit, clear message.
        target = self._new_target()
        proc = run_cli(os.path.join(self.workspace, "no_such.zip"), target)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(proc.stderr.strip())

        # ZIP path pointing at a directory -> non-zero exit.
        proc = run_cli(self.workspace, target)
        self.assertNotEqual(proc.returncode, 0)

        # Valid arguments -> normal execution.
        good_target = self._new_target()
        good_report = os.path.join(self.workspace, "report_cli.json")
        proc = run_cli(self.safe_zip, good_target, good_report)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(os.path.join(good_target, "readme.txt")))
        self.assertTrue(os.path.isfile(good_report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
