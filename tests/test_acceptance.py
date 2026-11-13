"""Acceptance tests for the Markdown hidden-text scanner.

Each test maps to one numbered requirement from the task brief.  The CLI is
exercised as a real subprocess (``python scan_md.py ...``) so exit codes,
stdout and the on-disk report are verified end to end.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scan_md.py"

ZWSP = "​"
ZWNJ = "‌"
TAG = lambda text: "".join(chr(0xE0000 + ord(ch)) for ch in text)  # noqa: E731


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    # -I: isolated mode, so the scanner never imports a module planted next to
    # the samples it is asked to scan.
    return subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class Workspace:
    """Temporary directory holding a rule config, a corpus and a report path."""

    def __init__(self, testcase: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "docs"
        self.corpus.mkdir()
        self.report = self.root / "report.json"
        self.config = self.root / "rules.json"

    def write_config(self, rules, *, raw: str | None = None) -> Path:
        if raw is not None:
            self.config.write_text(raw, encoding="utf-8")
        else:
            self.config.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        return self.config

    def write_doc(self, name: str, text: str) -> Path:
        path = self.corpus / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def write_bytes(self, name: str, data: bytes) -> Path:
        path = self.corpus / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def scan(self, *, directory: Path | None = None, config: Path | None = None,
             output: Path | None = None) -> subprocess.CompletedProcess[str]:
        return run_cli(
            "--dir", str(directory or self.corpus),
            "--config", str(config or self.config),
            "--output", str(output or self.report),
        )

    def load_report(self, path: Path | None = None):
        return json.loads((path or self.report).read_text(encoding="utf-8"))


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Workspace(self)

    def by_file(self, report, name: str) -> dict:
        for row in report:
            if row["file"] == name:
                return row
        self.fail(f"{name!r} not in report: {[r['file'] for r in report]}")


# --------------------------------------------------------------------------- #
# 1. recursion + extension filter
# --------------------------------------------------------------------------- #
class TestDiscovery(BaseCase):
    def test_only_md_files_are_reported(self):
        self.ws.write_config([{"name": "eval", "keywords": ["eval"]}])
        self.ws.write_doc("a.md", "safe content\n")
        (self.ws.corpus / "b.txt").write_text("eval\n", encoding="utf-8")
        (self.ws.corpus / "c.markdown").write_text("eval\n", encoding="utf-8")
        (self.ws.corpus / "d.md.txt").write_text("eval\n", encoding="utf-8")

        result = self.ws.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row["file"] for row in self.ws.load_report()], ["a.md"])

    def test_recursive_scan_and_case_insensitive_extension(self):
        self.ws.write_config([{"name": "eval", "keywords": ["eval"]}])
        self.ws.write_doc("top.md", "plain\n")
        self.ws.write_doc("nested/deep/inner.md", "plain\n")
        self.ws.write_doc("nested/UPPER.MD", "plain\n")
        self.ws.write_doc("nested/skip.txt", "plain\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        self.assertEqual(
            [row["file"] for row in self.ws.load_report()],
            ["nested/UPPER.MD", "nested/deep/inner.md", "top.md"],
        )


# --------------------------------------------------------------------------- #
# 2./3./4. keyword and regex rules
# --------------------------------------------------------------------------- #
class TestRules(BaseCase):
    def test_keyword_rule_hit(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("doc.md", "the config calls eval() at startup\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])
        self.assertEqual(row["matched_rules"], ["code-exec"])
        self.assertIsNone(row["error"])

    def test_keyword_is_case_insensitive(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("doc.md", "EVALUATE the expression with EVAL\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])
        self.assertIn("code-exec", row["matched_rules"])

    def test_regex_rule_records_rule_name(self):
        self.ws.write_config([
            {"name": "injection", "patterns": [r"(?i)ignore\s+previous\s+instructions"]},
        ])
        self.ws.write_doc("doc.md", "Please IGNORE   previous  instructions now.\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])
        self.assertEqual(row["matched_rules"], ["injection"])

    def test_no_hit_reports_clean(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("doc.md", "ordinary product documentation\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertFalse(row["suspicious"])
        self.assertEqual(row["matched_rules"], [])
        self.assertIsNone(row["error"])

    def test_all_matching_rules_are_listed(self):
        self.ws.write_config([
            {"name": "rule-a", "keywords": ["secret"]},
            {"name": "rule-b", "patterns": [r"secret"]},
            {"name": "rule-c", "keywords": ["absent"]},
        ])
        self.ws.write_doc("doc.md", "the secret token\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertEqual(row["matched_rules"], ["rule-a", "rule-b"])

    def test_invalid_regex_is_a_config_error(self):
        self.ws.write_config([{"name": "bad", "patterns": ["(unclosed"]}])
        result = self.ws.scan()
        self.assertEqual(result.returncode, 2)
        self.assertIn("regular expression", result.stderr)
        self.assertFalse(self.ws.report.exists())


# --------------------------------------------------------------------------- #
# 5. HTML comments and zero-width smuggling
# --------------------------------------------------------------------------- #
class TestHiddenContent(BaseCase):
    def _config(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])

    def test_keyword_inside_html_comment(self):
        self._config()
        self.ws.write_doc("doc.md", "# Visible title\n\n<!-- eval(cfg) -->\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])
        self.assertEqual(row["matched_rules"], ["code-exec"])

    def test_keyword_split_by_zero_width_characters(self):
        self._config()
        self.ws.write_doc("doc.md", f"配置会执行 ev{ZWSP}al(cfg) 计算默认值\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"], "raw text contains no 'eval' but rendered text does")

    def test_keyword_split_by_unicode_tag_smuggling(self):
        self._config()
        # tag characters are invisible in every renderer; a naive raw scan
        # sees only the surrounding CJK text.
        self.ws.write_doc("doc.md", f"正常说明 {TAG('eval')} 结束\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])

    def test_fullwidth_obfuscation(self):
        self._config()
        self.ws.write_doc("doc.md", "调用 ｅｖａｌ 解析表达式\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertTrue(row["suspicious"])

    def test_rendered_safe_but_comment_payload_flagged(self):
        self.ws.write_config([
            {"name": "hidden-comment", "patterns": [r"<!--(?s:.*?)eval(?s:.*?)-->"]},
        ])
        self.ws.write_doc("doc.md", "Rendered text is harmless.\n\n<!-- eval(x) -->\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        self.assertTrue(self.by_file(self.ws.load_report(), "doc.md")["suspicious"])

    def test_ordinary_emoji_zwj_sequence_is_not_noise(self):
        # A ZWJ emoji sequence must not produce a keyword hit by accident.
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("doc.md", "# 团队 👨‍👩‍👧‍👦\n\n我们欢迎所有访客。\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        row = self.by_file(self.ws.load_report(), "doc.md")
        self.assertFalse(row["suspicious"])


# --------------------------------------------------------------------------- #
# 6./7. report schema and ordering
# --------------------------------------------------------------------------- #
class TestReport(BaseCase):
    def test_schema_fields_and_ordering(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("B.md", "eval(x)\n")
        self.ws.write_doc("a.md", "clean\n")
        self.ws.write_doc("nested/C.md", "clean\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        report = self.ws.load_report()
        self.assertEqual([row["file"] for row in report], ["B.md", "a.md", "nested/C.md"])
        for row in report:
            self.assertEqual(list(row), ["file", "suspicious", "matched_rules", "error"])
            self.assertIsInstance(row["suspicious"], bool)
            self.assertIsInstance(row["matched_rules"], list)
            self.assertTrue(row["error"] is None or isinstance(row["error"], str))

        self.assertIsNone(report[1]["error"])
        self.assertEqual(report[0]["matched_rules"], ["code-exec"])

    def test_output_is_stable_across_runs(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        for name in ("z.md", "A.md", "m/n.md", "B.md"):
            self.ws.write_doc(name, "eval\n" if name == "B.md" else "ok\n")

        self.assertEqual(self.ws.scan().returncode, 0)
        first = self.ws.report.read_text(encoding="utf-8")
        self.assertEqual(self.ws.scan().returncode, 0)
        second = self.ws.report.read_text(encoding="utf-8")
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# 8./9. configuration and directory errors
# --------------------------------------------------------------------------- #
class TestFailureModes(BaseCase):
    def test_missing_config_exits_non_zero(self):
        self.ws.write_doc("a.md", "text\n")
        result = self.ws.scan(config=self.ws.root / "does-not-exist.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("config file not found", result.stderr)
        self.assertFalse(self.ws.report.exists())

    def test_invalid_json_exits_non_zero(self):
        self.ws.write_doc("a.md", "text\n")
        self.ws.write_config(None, raw='{"rules": [')
        result = self.ws.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid JSON", result.stderr)
        self.assertFalse(self.ws.report.exists())

    def test_missing_directory_exits_non_zero_without_report(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        result = self.ws.scan(directory=self.ws.root / "no-such-dir")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(self.ws.report.exists())

    def test_missing_required_argument(self):
        result = run_cli("--dir", str(self.ws.corpus), "--output", str(self.ws.report))
        self.assertEqual(result.returncode, 2)

    def test_unwritable_report_location_exits_non_zero(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("a.md", "eval\n")
        # A *file* where the report's parent directory should be: mkdir fails.
        blocker = self.ws.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")

        result = self.ws.scan(output=blocker / "report.json")
        self.assertEqual(result.returncode, 4)
        self.assertIn("cannot write report", result.stderr)
        self.assertFalse((blocker / "report.json").exists())


# --------------------------------------------------------------------------- #
# 10. per-file read failure
# --------------------------------------------------------------------------- #
class TestUnreadableFile(BaseCase):
    def _make_unreadable(self, path: Path) -> bool:
        """Deny read access; return False when the platform cannot enforce it."""
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            proc = subprocess.run(
                ["icacls", str(path), "/deny", f"{user}:(R)"],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return False
            self.addCleanup(
                lambda: subprocess.run(
                    ["icacls", str(path), "/remove:d", user], capture_output=True, text=True,
                )
            )
        else:
            if os.geteuid() == 0:  # type: ignore[attr-defined]
                return False
            path.chmod(0o000)
            self.addCleanup(path.chmod, 0o644)

        try:
            path.read_bytes()
        except PermissionError:
            return True
        return False

    def test_unreadable_file_is_reported_and_scan_continues(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        locked = self.ws.write_doc("locked.md", "eval(x)\n")
        self.ws.write_doc("ok.md", "eval(x)\n")
        self.ws.write_doc("clean.md", "nothing here\n")

        if not self._make_unreadable(locked):
            self.skipTest("platform does not enforce read denial for this user")

        result = self.ws.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.ws.load_report()

        locked_row = self.by_file(report, "locked.md")
        self.assertIsNotNone(locked_row["error"])
        self.assertIn("denied", locked_row["error"].lower())
        self.assertTrue(locked_row["suspicious"], "unreadable files are fail-closed")

        ok_row = self.by_file(report, "ok.md")
        self.assertIsNone(ok_row["error"])
        self.assertEqual(ok_row["matched_rules"], ["code-exec"])
        self.assertIsNone(self.by_file(report, "clean.md")["error"])


# --------------------------------------------------------------------------- #
# 11. CLI summary
# --------------------------------------------------------------------------- #
class TestCliSummary(BaseCase):
    def test_summary_matches_report(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("hit.md", "eval(x)\n")
        self.ws.write_doc("clean.md", "nothing\n")
        self.ws.write_doc("nested/also-clean.md", "nothing\n")

        result = self.ws.scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scanned 3 markdown file(s)", result.stdout)
        self.assertIn("1 suspicious, 2 clean", result.stdout)

        report = self.ws.load_report()
        self.assertEqual(len(report), 3)
        self.assertEqual(sum(row["suspicious"] for row in report), 1)

    def test_output_directory_is_created(self):
        self.ws.write_config([{"name": "code-exec", "keywords": ["eval"]}])
        self.ws.write_doc("a.md", "eval\n")
        output = self.ws.root / "out" / "nested" / "report.json"

        result = self.ws.scan(output=output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())


# --------------------------------------------------------------------------- #
# Unit-level checks
# --------------------------------------------------------------------------- #
class TestUnits(unittest.TestCase):
    def test_decode_gbk_fallback(self):
        from mdscan.documents import decode_document

        text, error = decode_document("中文说明".encode("gb18030"))
        self.assertIsNone(error)
        self.assertEqual(text, "中文说明")

    def test_decode_failure_is_reported(self):
        from mdscan.documents import decode_document

        # 0xFF is not a valid lead byte in UTF-8 and cannot start a GB18030
        # sequence either, so no encoding candidate succeeds.
        text, error = decode_document(b"ok\xff\xff\xff\xff")
        self.assertEqual(text, "")
        self.assertIsNotNone(error)
        self.assertIn("cannot decode", error)

    def test_config_rejects_empty_rule_list(self):
        from mdscan.config import ConfigError, parse_config

        with self.assertRaises(ConfigError):
            parse_config({"rules": []})

    def test_config_accepts_bare_array(self):
        from mdscan.config import parse_config

        rules = parse_config([{"name": "r", "keywords": ["x"]}])
        self.assertEqual([r.name for r in rules], ["r"])

    def test_empty_file_is_clean(self):
        from mdscan.config import parse_config
        from mdscan.detector import evaluate_document

        rules = parse_config([{"name": "r", "keywords": ["eval"]}])
        self.assertEqual(evaluate_document("", rules), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
