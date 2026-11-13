"""End-to-end tests for the secscan CLI.

Run with:  python -m unittest discover -s tests -v
No third-party packages are required.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "secscan.py"
SAMPLES = ROOT / "samples"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-I", str(CLI), *args],
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )


class Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="secscan-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def load_vulnerable(self) -> dict:
        return self.read_json(SAMPLES / "vulnerable.json")

    def scan(self, config: Path, output: Path, *extra: str):
        return run_cli("--config", str(config), "--output", str(output), *extra)


class TestArgumentHandling(Workspace):
    """Requirement 10: required CLI flags and usage output."""

    def test_no_arguments_fails_with_usage(self) -> None:
        result = run_cli()
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        for flag in ("--config", "--output", "--fix", "--rules"):
            self.assertIn(flag, combined)
        self.assertIn("usage", combined.lower())

    def test_missing_output_fails(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        result = run_cli("--config", str(config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--output", result.stderr)

    def test_help_lists_all_parameters(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for flag in ("--config", "--output", "--fix", "--rules"):
            self.assertIn(flag, result.stdout)


class TestInputFailures(Workspace):
    """Requirement 1: missing or malformed config, non-zero, no report."""

    def test_missing_config(self) -> None:
        output = self.tmp / "report.json"
        missing = self.tmp / "does-not-exist.json"
        result = run_cli("--config", str(missing), "--output", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("不存在", result.stderr)
        self.assertIn("not found", result.stderr)
        self.assertFalse(output.exists(), "no report may be produced on failure")

    def test_malformed_json(self) -> None:
        config = self.tmp / "broken.json"
        config.write_text('{"password": "123456",}', encoding="utf-8")
        output = self.tmp / "report.json"
        result = run_cli("--config", str(config), "--output", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("非法", result.stderr)
        self.assertIn("invalid JSON", result.stderr)
        self.assertFalse(output.exists())

    def test_scalar_root_rejected(self) -> None:
        config = self.write_json("scalar.json", [1, 2, 3])
        output = self.tmp / "report.json"
        # A top-level array is valid; an array of scalars simply has nothing
        # to evaluate, so this should succeed but produce an empty report.
        result = run_cli("--config", str(config), "--output", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["total"], 0)

        config2 = self.tmp / "string.json"
        config2.write_text('"just a string"', encoding="utf-8")
        result = run_cli("--config", str(config2), "--output", str(self.tmp / "r2.json"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "r2.json").exists())


class TestDetection(Workspace):
    """Requirements 2, 3, 4, 8."""

    def test_weak_password_high_with_path(self) -> None:
        config = self.write_json("cfg.json", {"app": {"password": "123456"}})
        output = self.tmp / "report.json"
        result = self.scan(config, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["total"], 1)
        entry = report["vulnerabilities"][0]
        self.assertEqual(entry["severity"], "高危")
        self.assertEqual(entry["rule_type"], "weak_password")
        self.assertEqual(entry["path"], "$.app.password")
        self.assertEqual(entry["bare_path"], "app.password")

    def test_plaintext_secret_medium_and_description(self) -> None:
        config = self.write_json("cfg.json", {"app": {"secret": "abc"}})
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        entry = report["vulnerabilities"][0]
        self.assertEqual(entry["severity"], "中危")
        self.assertEqual(entry["path"], "$.app.secret")
        self.assertIn("明文存储", entry["description"])
        self.assertNotIn("abc", json.dumps(entry), "raw secret must not leak")

    def test_dangerous_loader_high(self) -> None:
        config = self.write_json("cfg.json", {"loader": "yaml.load"})
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        entry = report["vulnerabilities"][0]
        self.assertEqual(entry["severity"], "高危")
        self.assertEqual(entry["rule_type"], "dangerous_deserialization")

    def test_safe_loader_and_pickle_variants(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "a": {"loader": "yaml.SafeLoader"},
                "b": {"loader": "yaml.safe_load"},
                "c": {"loader": "yaml.load(stream, Loader=yaml.SafeLoader)"},
                "d": {"loader": "pickle.loads"},
                "e": {"loader": "json.loads"},
            },
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["vulnerabilities"][0]["path"], "$.d.loader")

    def test_loader_spec_under_arbitrary_key(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "a": {"format": "pickle.loads"},
                "b": {"engine": "yaml.load"},
                "c": {"engine": "yaml.load(stream, Loader=yaml.SafeLoader)"},
                "d": {"note": "never call pickle.loads on untrusted input"},
                "e": {"url": "https://example.test/yaml.load"},
            },
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(paths, ["$.a.format", "$.b.engine"])

    def test_non_dictionary_password_is_not_flagged(self) -> None:
        config = self.write_json(
            "cfg.json", {"password": "Tr0ub4dor&3-very-long", "pwd": "123456"}
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(paths, ["$.pwd"])

    def test_unknown_nested_structure_skipped_but_scan_continues(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "foo": {"bar": 1},
                "app": {"password": "123456", "secret": "abc"},
            },
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        self.assertEqual(report["total"], 2)
        self.assertIn("$.foo", report["skipped_paths"])
        self.assertIn("foo", report["skipped_paths_bare"])
        skipped_entry = next(e for e in report["skipped"] if e["path"] == "$.foo")
        self.assertEqual(skipped_entry["kind"], "nested")
        self.assertIn("复核", skipped_entry["reason"])
        # The rest of the findings remain intact.
        types = {entry["rule_type"] for entry in report["vulnerabilities"]}
        self.assertEqual(types, {"weak_password", "plaintext_secret"})

    def test_nested_lists_and_complex_keys(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "servers": [
                    {"name": "a", "db": {"password": "123456"}},
                    {"name": "b", "db": {"password": "S7r0ng!Passw0rd#2024"}},
                ],
                "odd key.with dots": {"secret": "abc"},
                "password": ["123456", "uncommon-pw-1"],
            },
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertIn("$.servers[0].db.password", paths)
        self.assertIn("$['odd key.with dots'].secret", paths)
        self.assertIn("$.password[0]", paths)
        self.assertNotIn("$.servers[1].db.password", paths)

    def test_protected_reference_not_flagged_as_plaintext(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "secret": "${VAULT_SECRET}",
                "token": "vault://prod/token",
                "api_key": "ENC(abcdef)",
                "client_secret": "plain-value",
            },
        )
        output = self.tmp / "report.json"
        self.scan(config, output)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(paths, ["$.client_secret"])


class TestReportDeterminism(Workspace):
    """Requirement 5: total == len(list), stable ordering across runs."""

    def test_total_matches_and_order_is_stable(self) -> None:
        config = self.write_json("cfg.json", self.load_vulnerable())
        out1 = self.tmp / "r1.json"
        out2 = self.tmp / "r2.json"
        self.scan(config, out1)
        self.scan(config, out2)
        first = self.read_json(out1)
        second = self.read_json(out2)
        self.assertEqual(first["total"], len(first["vulnerabilities"]))
        self.assertEqual(first["total"], 4)
        self.assertEqual(
            [entry["path"] for entry in first["vulnerabilities"]],
            [entry["path"] for entry in second["vulnerabilities"]],
        )
        self.assertEqual(first["vulnerabilities"], second["vulnerabilities"])

    def test_sorted_order_is_lexicographic_by_path(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "zeta": {"token": "t"},
                "alpha": {"password": "123456"},
                "mid": {"secret": "s"},
            },
        )
        output = self.tmp / "r.json"
        self.scan(config, output)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(
            paths,
            ["$.alpha.password", "$.mid.secret", "$.zeta.token"],
        )


class TestFixMode(Workspace):
    """Requirements 6, 7."""

    def test_fix_clears_fixable_and_keeps_loader(self) -> None:
        document = self.load_vulnerable()
        config = self.write_json("cfg.json", document)
        fixed = self.tmp / "cfg.fixed.json"
        output = self.tmp / "report.json"

        run = self.scan(config, output, "--fix", "--fixed-config", str(fixed))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("复扫验证通过", run.stdout)

        report = self.read_json(output)
        self.assertEqual(report["mode"], "fix")
        self.assertEqual(report["pre_fix_total"], 4)
        self.assertEqual(report["post_fix_total"], 1)
        self.assertEqual(report["total"], 1)
        remaining = report["vulnerabilities"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["rule_type"], "dangerous_deserialization")
        self.assertEqual(remaining[0]["path"], "$.app.serialization.loader")

        fixed_doc = self.read_json(fixed)
        # Nested level and untouched values preserved.
        self.assertEqual(fixed_doc["app"]["port"], document["app"]["port"])
        self.assertEqual(fixed_doc["app"]["database"]["host"], "db.internal.example")
        self.assertEqual(
            fixed_doc["app"]["integrations"]["notification"]["retry"], 3
        )
        self.assertEqual(fixed_doc["foo"], {"bar": 1})
        self.assertEqual(fixed_doc["feature_flags"], {"new_checkout": True, "beta_search": False})
        self.assertEqual(
            fixed_doc["app"]["workers"], document["app"]["workers"]
        )
        # Vulnerable values replaced.
        self.assertNotEqual(fixed_doc["app"]["database"]["password"], "123456")
        self.assertNotEqual(fixed_doc["app"]["integrations"]["payment_gateway"]["secret"], "abc")
        self.assertEqual(fixed_doc["app"]["serialization"]["loader"], "yaml.load")

    def test_fix_preserves_all_non_vulnerable_leaves(self) -> None:
        document = self.load_vulnerable()
        config = self.write_json("cfg.json", document)
        fixed = self.tmp / "cfg.fixed.json"
        self.scan(config, self.tmp / "report.json", "--fix", "--fixed-config", str(fixed))
        fixed_doc = self.read_json(fixed)

        def flatten(node, prefix=""):
            out = {}
            if isinstance(node, dict):
                for key, value in node.items():
                    out.update(flatten(value, f"{prefix}/{key}"))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    out.update(flatten(value, f"{prefix}[{index}]"))
            else:
                out[prefix] = node
            return out

        before = flatten(document)
        after = flatten(fixed_doc)
        vulnerable = {
            "/app/database/password",
            "/app/integrations/payment_gateway/secret",
            "/app/integrations/notification/token",
        }
        self.assertEqual(set(before), set(after), "structure changed")
        for path in set(before) - vulnerable:
            self.assertEqual(before[path], after[path], f"value changed at {path}")
        for path in vulnerable:
            self.assertNotEqual(before[path], after[path])

    def test_default_fixed_path_is_next_to_config(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        output = self.tmp / "report.json"
        result = self.scan(config, output, "--fix")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.tmp / "cfg.fixed.json"
        self.assertTrue(expected.exists())
        fixed_doc = self.read_json(expected)
        self.assertNotEqual(fixed_doc["password"], "123456")

    def test_fixed_config_requires_fix_flag(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        result = self.scan(
            config, self.tmp / "r.json", "--fixed-config", str(self.tmp / "x.json")
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--fix", result.stderr)

    def test_report_must_not_overwrite_config(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        result = self.scan(config, config)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_json(config)["password"], "123456")

    def test_fixed_output_must_not_overwrite_config(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        result = self.scan(
            config, self.tmp / "r.json", "--fix", "--fixed-config", str(config)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read_json(config)["password"], "123456")
        self.assertFalse((self.tmp / "r.json").exists())


class TestRulesFile(Workspace):
    """Requirement 9: optional rules; missing file warns and falls back."""

    def test_missing_rules_file_warns_and_scans(self) -> None:
        config = self.write_json("cfg.json", {"app": {"password": "123456"}})
        output = self.tmp / "report.json"
        missing = self.tmp / "no-such-rules.json"
        result = self.scan(config, output, "--rules", str(missing))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("使用内置默认规则", result.stderr)
        self.assertIn("default rules", result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["rules"]["source"], "builtin-default")
        self.assertEqual(report["total"], 1)

    def test_custom_rules_override_detection(self) -> None:
        config = self.write_json(
            "cfg.json",
            {
                "app": {
                    "password": "Company@2024",
                    "secret": "abc",
                    "svc_password": "ordersvc",
                }
            },
        )
        output = self.tmp / "report.json"
        result = self.scan(config, output, "--rules", str(SAMPLES / "rules.json"))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["rules"]["source"], "file")
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(
            paths,
            ["$.app.password", "$.app.secret", "$.app.svc_password"],
        )
        severities = {entry["path"]: entry["severity"] for entry in report["vulnerabilities"]}
        self.assertEqual(severities["$.app.secret"], "中危")

    def test_invalid_rules_file_fails_without_report(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        rules = self.tmp / "bad-rules.json"
        rules.write_text("{not json", encoding="utf-8")
        output = self.tmp / "report.json"
        result = self.scan(config, output, "--rules", str(rules))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("规则文件", result.stderr)
        self.assertFalse(output.exists())


class TestRobustness(Workspace):
    def test_deeply_nested_config_does_not_crash(self) -> None:
        depth = 400  # beyond MAX_SCAN_DEPTH but still parseable by json
        text = '{"n":' * depth + '{"password": "123456"}' + "}" * depth
        config = self.tmp / "deep.json"
        config.write_text(text, encoding="utf-8")
        output = self.tmp / "report.json"
        result = self.scan(config, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["total"], 0)
        self.assertIn("$.n", report["skipped_paths"])
        self.assertIn("MAX_SCAN_DEPTH", report["skipped"][0]["reason"])

    def test_extremely_deep_config_fails_or_caps_cleanly(self) -> None:
        # Depending on the interpreter's C-level JSON recursion limit, a
        # 5000-deep document either fails to parse or parses and then hits
        # our own depth cap. Neither path may crash or leave a partial report.
        depth = 5000
        text = '{"n":' * depth + "1" + "}" * depth
        config = self.tmp / "too-deep.json"
        config.write_text(text, encoding="utf-8")
        output = self.tmp / "report.json"
        result = self.scan(config, output)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("未预期错误", result.stderr)
        if result.returncode == 0:
            report = self.read_json(output)
            self.assertEqual(report["total"], 0)
            self.assertIn("MAX_SCAN_DEPTH", report["skipped"][0]["reason"])
        else:
            self.assertEqual(result.returncode, 3)
            self.assertIn("嵌套层级过深", result.stderr)
            self.assertFalse(output.exists())

    def test_unicode_and_duplicate_keys(self) -> None:
        config = self.tmp / "cfg.json"
        config.write_text(
            '{"配置": {"password": "123456"}, "secret": "abc", "secret": "def"}',
            encoding="utf-8",
        )
        output = self.tmp / "report.json"
        result = self.scan(config, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        paths = [entry["path"] for entry in report["vulnerabilities"]]
        self.assertEqual(paths, ["$.secret", "$.配置.password"])

    def test_output_directory_missing(self) -> None:
        config = self.write_json("cfg.json", {"password": "123456"})
        result = self.scan(config, self.tmp / "nope" / "report.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("目录不存在", result.stderr)


class TestCleanSample(Workspace):
    def test_clean_config_has_no_findings(self) -> None:
        output = self.tmp / "report.json"
        result = self.scan(SAMPLES / "clean.json", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(output)
        self.assertEqual(report["total"], 0)
        self.assertEqual(report["vulnerabilities"], [])
        self.assertIn("$.app", report["skipped_paths"])

    def test_fix_then_rescan_yields_only_loader(self) -> None:
        """Requirement 6 end-to-end: re-scan the fixed file from disk."""

        config = self.write_json(
            "cfg.json",
            {
                "db": {"password": "123456"},
                "api": {"secret": "abc"},
                "ser": {"loader": "yaml.load"},
            },
        )
        fixed = self.tmp / "cfg.fixed.json"
        first = self.tmp / "first.json"
        self.scan(config, first, "--fix", "--fixed-config", str(fixed))

        second = self.tmp / "second.json"
        result = self.scan(fixed, second)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.read_json(second)
        self.assertEqual(report["total"], 1)
        self.assertEqual(
            [entry["path"] for entry in report["vulnerabilities"]],
            ["$.ser.loader"],
        )
        self.assertEqual(
            {entry["rule_type"] for entry in report["vulnerabilities"]},
            {"dangerous_deserialization"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
