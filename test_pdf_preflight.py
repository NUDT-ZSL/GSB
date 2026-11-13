"""Acceptance tests for the PDF preflight scanner.

Run with:  py -m unittest -v

Every sample is built deterministically in a temporary directory; nothing is
read from the network or an external attachment.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import demo_samples
import pdf_preflight as scanner


def _write(root: Path, relative: str, data: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _report_for(root: Path) -> dict:
    return scanner.build_report(root)


def _entry(report: dict, path: str) -> dict:
    for entry in report["files"]:
        if entry["path"] == path:
            return entry
    raise AssertionError(f"missing report entry for {path}")


class TestScanLayout(unittest.TestCase):
    """Requirement 1: recursive, case-insensitive .pdf discovery."""

    def test_only_pdf_files_reported_with_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.PDF", demo_samples.make_normal_pdf())
            _write(root, "b.txt", b"ignore me")
            _write(root, "sub/c.pdf", demo_samples.make_normal_pdf())
            _write(root, "sub/deeper/d.PdF", demo_samples.make_normal_pdf())
            _write(root, "sub/e.txt", b"ignore me too")
            (root / "not-a-file-dir").mkdir()

            report = _report_for(root)
            paths = [entry["path"] for entry in report["files"]]

            self.assertEqual(paths, ["a.PDF", "sub/c.pdf", "sub/deeper/d.PdF"])
            self.assertEqual(report["summary"]["total"], 3)


class TestScriptFeature(unittest.TestCase):
    """Requirement 2: javascript deduplicated per file."""

    def test_two_script_markers_yield_one_feature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "twice.pdf", demo_samples.make_script_pdf(markers=2))
            entry = _entry(_report_for(root), "twice.pdf")

            self.assertEqual(entry["features"], ["javascript"])


class TestAutoActionFeature(unittest.TestCase):
    """Requirement 3: auto_action recorded independently of javascript."""

    def test_script_and_auto_action_are_both_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "both.pdf",
                demo_samples.make_script_and_auto_pdf(),
            )
            entry = _entry(_report_for(root), "both.pdf")

            self.assertEqual(
                entry["features"],
                ["javascript", "auto_action"],
            )
            self.assertEqual(entry["level"], "high")

    def test_pure_auto_action_is_not_reported_as_javascript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "goto.pdf", demo_samples.make_auto_action_pdf())
            entry = _entry(_report_for(root), "goto.pdf")

            self.assertEqual(entry["features"], ["auto_action"])


class TestEmbeddedExecutable(unittest.TestCase):
    """Requirement 4: only .exe/.dll attachments count, case-insensitive."""

    def test_executable_attachment_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "exe.pdf", demo_samples.make_embedded_pdf("A.EXE"))
            _write(root, "dll.pdf", demo_samples.make_embedded_pdf("lib.Dll"))
            _write(root, "txt.pdf", demo_samples.make_embedded_pdf("readme.txt"))

            report = _report_for(root)
            self.assertIn(
                "embedded_executable", _entry(report, "exe.pdf")["features"]
            )
            self.assertIn(
                "embedded_executable", _entry(report, "dll.pdf")["features"]
            )
            self.assertNotIn(
                "embedded_executable", _entry(report, "txt.pdf")["features"]
            )

    def test_hex_encoded_executable_name_detected(self) -> None:
        # /UF <FEFF0061002E004500580045> is the UTF-16BE form of "a.EXE".
        data = demo_samples.make_embedded_pdf("x").replace(
            b"/UF (x)",
            b"/UF <FEFF0061002E004500580045>",
        )
        self.assertIn(
            "embedded_executable",
            scanner.detect_features(data),
        )


class TestRiskLevels(unittest.TestCase):
    """Requirement 5: level = number of distinct feature kinds."""

    def test_zero_one_three_feature_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "zero.pdf", demo_samples.make_normal_pdf())
            _write(root, "one.pdf", demo_samples.make_script_pdf())
            _write(root, "three.pdf", demo_samples.make_all_features_pdf())

            report = _report_for(root)
            self.assertEqual(_entry(report, "zero.pdf")["level"], "low")
            self.assertEqual(_entry(report, "one.pdf")["level"], "medium")
            self.assertEqual(_entry(report, "three.pdf")["level"], "high")

    def test_level_matches_feature_count_for_every_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            demo_samples.write_demo_tree(Path(tmp) / "incoming")
            report = _report_for(Path(tmp) / "incoming")
            for entry in report["files"]:
                kinds = len(entry["features"])
                if entry["level"] == "unknown":
                    self.assertEqual(entry["features"], [])
                    continue
                expected = "low" if kinds == 0 else "medium" if kinds == 1 else "high"
                self.assertEqual(entry["level"], expected, entry["path"])


class TestOrdering(unittest.TestCase):
    """Requirement 6: high -> medium -> low -> unknown, path ascending."""

    def test_ordering_by_level_then_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "x.pdf", demo_samples.make_all_features_pdf())
            _write(root, "y.pdf", demo_samples.make_all_features_pdf())
            _write(root, "m.pdf", demo_samples.make_auto_action_pdf())
            _write(root, "z.pdf", demo_samples.make_normal_pdf())
            _write(root, "bad.pdf", demo_samples.make_broken_pdf())

            report = _report_for(root)
            paths = [entry["path"] for entry in report["files"]]

            self.assertEqual(paths, ["x.pdf", "y.pdf", "m.pdf", "z.pdf", "bad.pdf"])
            levels = [entry["level"] for entry in report["files"]]
            self.assertEqual(
                levels, ["high", "high", "medium", "low", "unknown"]
            )


class TestBrokenFiles(unittest.TestCase):
    """Requirement 7: unparsable -> unknown, empty features, no guessing."""

    def test_broken_file_does_not_affect_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "broken.pdf", demo_samples.make_broken_pdf())
            _write(root, "clean.pdf", demo_samples.make_normal_pdf())
            _write(root, "evil.pdf", demo_samples.make_all_features_pdf())
            _write(root, "text.pdf", demo_samples.make_not_a_pdf())

            report = _report_for(root)

            broken = _entry(report, "broken.pdf")
            self.assertEqual(broken["level"], "unknown")
            self.assertEqual(broken["features"], [])
            self.assertEqual(broken["status"], "unparsed")
            self.assertIn("reason", broken)

            text = _entry(report, "text.pdf")
            self.assertEqual(text["level"], "unknown")
            self.assertEqual(text["features"], [])

            self.assertEqual(_entry(report, "clean.pdf")["level"], "low")
            self.assertEqual(_entry(report, "evil.pdf")["level"], "high")

    def test_encrypted_file_is_unknown_and_not_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "enc.pdf",
                demo_samples.make_encrypted_pdf(with_script=True),
            )
            entry = _entry(_report_for(root), "enc.pdf")

            self.assertEqual(entry["level"], "unknown")
            self.assertEqual(entry["features"], [])
            self.assertEqual(entry["reason"], "encrypted")

    def test_xref_stream_file_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "modern.pdf", demo_samples.make_xref_stream_pdf())
            entry = _entry(_report_for(root), "modern.pdf")

            self.assertEqual(entry["level"], "high")
            self.assertEqual(
                entry["features"], ["javascript", "auto_action"]
            )

    def test_features_inside_object_stream_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "packed.pdf",
                demo_samples.make_compressed_objects_pdf(),
            )
            entry = _entry(_report_for(root), "packed.pdf")

            self.assertEqual(entry["level"], "high")
            self.assertEqual(
                entry["features"],
                ["javascript", "embedded_executable"],
            )


class TestSummary(unittest.TestCase):
    """Requirement 8: summary counts equal files and sum to total."""

    def test_summary_matches_expected_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "high.pdf", demo_samples.make_all_features_pdf())
            _write(root, "medium.pdf", demo_samples.make_script_pdf())
            _write(root, "low.pdf", demo_samples.make_normal_pdf())
            _write(root, "broken.pdf", demo_samples.make_broken_pdf())

            summary = _report_for(root)["summary"]
            self.assertEqual(summary["total"], 4)
            self.assertEqual(summary["low"], 1)
            self.assertEqual(summary["medium"], 1)
            self.assertEqual(summary["high"], 1)
            self.assertEqual(summary["unknown"], 1)

    def test_summary_is_consistent_for_demo_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            demo_samples.write_demo_tree(Path(tmp) / "incoming")
            report = _report_for(Path(tmp) / "incoming")
            summary = report["summary"]

            self.assertEqual(summary["total"], len(report["files"]))
            self.assertEqual(
                summary["total"],
                summary["low"]
                + summary["medium"]
                + summary["high"]
                + summary["unknown"],
            )


class TestStructuralVariants(unittest.TestCase):
    """Valid layouts must not be misreported as unknown."""

    def test_multi_subsection_classic_xref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root,
                "split.pdf",
                demo_samples.make_multi_subsection_xref_pdf(),
            )
            entry = _entry(_report_for(root), "split.pdf")

            self.assertEqual(entry["status"], "parsed")
            self.assertEqual(entry["level"], "high")
            self.assertEqual(
                entry["features"], ["javascript", "auto_action"]
            )


class TestEndToEnd(unittest.TestCase):
    """The CLI writes a UTF-8 JSON report and returns exit code 0."""

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            incoming = demo_samples.write_demo_tree(workdir)
            report_path = workdir / "out" / "report.json"

            code = scanner.main([str(incoming), "-o", str(report_path)])
            self.assertEqual(code, 0)
            self.assertTrue(report_path.is_file())

            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("files", payload)
            self.assertIn("summary", payload)
            self.assertEqual(
                payload["summary"]["total"],
                len(payload["files"]),
            )

    def test_cli_missing_input_dir_returns_2(self) -> None:
        code = scanner.main(["does-not-exist", "-o", "unused.json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
