from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from config_order.canonical import canonical_dumps
from config_order.engine import build_chain, diagnose_scenario
from config_order.errors import E_BIND, E_ENV, E_CHAIN, LoadRejected
from config_order.loader import apply_injection, load_scenario, validate_scenario
from config_order.service import run_diagnose, run_restore
from config_order.verify import verify_session


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "samples" / "scenario.base.json"
REBIASED = ROOT / "samples" / "scenario.rebiased.json"


def key_index(result: dict) -> dict[tuple[str, str], dict]:
    return {
        (environment["environment_id"], key["key"]): key
        for environment in result["environments"]
        for key in environment["keys"]
    }


class Workspace:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.db = self.path / "diagnosis.sqlite"
        self.log = self.path / "diagnosis.jsonl"

    def close(self) -> None:
        self.temp.cleanup()


class LoaderValidationTests(unittest.TestCase):
    def test_valid_sample_normalizes_to_canonical_shape(self) -> None:
        scenario = load_scenario(BASE)
        self.assertEqual(
            scenario["environment_set_version"], "sample-set-2026-11-17"
        )
        self.assertEqual(
            [environment["id"] for environment in scenario["environments"]],
            ["dev", "prod", "staging"],
        )
        self.assertEqual(
            [source["id"] for source in scenario["sources"]],
            ["file_override", "local_baseline", "runtime_injection"],
        )

    def test_non_integer_bias_is_rejected_as_e_env_with_path(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        raw["environments"][0]["priority_bias"]["local_baseline"] = "high"
        with self.assertRaises(LoadRejected) as caught:
            validate_scenario(raw)
        self.assertEqual(caught.exception.business_code, E_ENV)
        paths = {issue.field_path for issue in caught.exception.issues}
        self.assertIn(
            "environments[0].priority_bias.local_baseline", paths
        )

    def test_duplicate_environment_is_rejected(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        raw["environments"][1]["id"] = raw["environments"][0]["id"]
        with self.assertRaises(LoadRejected) as caught:
            validate_scenario(raw)
        self.assertEqual(caught.exception.business_code, E_ENV)
        self.assertTrue(
            any("duplicate environment" in issue.message for issue in caught.exception.issues)
        )

    def test_missing_binding_source_is_e_bind_with_path(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        raw["environments"][0]["bindings"]["service.mode"] = {
            "require": ["missing_source"],
            "forbid": [],
        }
        with self.assertRaises(LoadRejected) as caught:
            validate_scenario(raw)
        self.assertEqual(caught.exception.business_code, E_BIND)
        self.assertEqual(
            caught.exception.issues[0].source_id, "missing_source"
        )
        self.assertIn(
            "bindings.service.mode.require[0]",
            caught.exception.issues[0].field_path,
        )

    def test_self_contradictory_binding_is_rejected(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        raw["environments"][2]["bindings"]["service.mode"] = {
            "require": ["file_override"],
            "forbid": ["file_override"],
        }
        with self.assertRaises(LoadRejected) as caught:
            validate_scenario(raw)
        self.assertEqual(caught.exception.business_code, E_ENV)
        self.assertTrue(
            any(
                "both required and forbidden" in issue.message
                for issue in caught.exception.issues
            )
        )


class DeterministicEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = load_scenario(BASE)
        cls.result = diagnose_scenario(cls.scenario)

    def test_repeated_chain_build_is_byte_identical(self) -> None:
        prod = next(
            env for env in self.scenario["environments"] if env["id"] == "prod"
        )
        first = build_chain(prod, "service.mode")
        second = build_chain(prod, "service.mode")
        self.assertEqual(canonical_dumps(first), canonical_dumps(second))

    def test_source_and_rule_array_order_do_not_change_chain_digest(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        raw["sources"].reverse()
        raw["environments"].reverse()
        for environment in raw["environments"]:
            environment["available_sources"].reverse()
            environment["order_rules"].reverse()
            for rules in environment["key_order_rules"].values():
                rules.reverse()
        permuted = validate_scenario(raw)
        original = validate_scenario(json.loads(BASE.read_text(encoding="utf-8")))
        original_result = diagnose_scenario(original)
        permuted_result = diagnose_scenario(permuted)
        self.assertEqual(
            canonical_dumps(original_result), canonical_dumps(permuted_result)
        )

    def test_cycle_and_conflict_primary_codes(self) -> None:
        keys = key_index(self.result)
        self.assertEqual(keys[("prod", "feature.order_probe")]["code"], "E_CYCLE")
        self.assertEqual(
            keys[("prod", "feature.conflict_probe")]["code"], "E_CONFLICT"
        )
        self.assertEqual(
            keys[("prod", "feature.required_probe")]["code"], "E_CONFLICT"
        )
        cycle_decision = keys[("prod", "feature.order_probe")]["decision"]
        self.assertIn("rule_basis", cycle_decision["trace"][1]["detail"])
        self.assertTrue(cycle_decision["trace"][1]["detail"]["rule_basis"])

    def test_binding_coverage_has_two_required_reason_classes(self) -> None:
        keys = key_index(self.result)
        endpoint = keys[("prod", "service.endpoint")]["decision"]
        self.assertEqual(endpoint["selected_source"], "file_override")
        reasons = {item["source"]: item["reason"] for item in endpoint["coverage"]}
        self.assertEqual(reasons["runtime_injection"], "BINDING_FORBIDDEN")
        self.assertEqual(reasons["local_baseline"], "LOWER_PRIORITY")

    def test_non_monotonic_bias_diagnostic_is_exposed(self) -> None:
        keys = key_index(self.result)
        chain = keys[("prod", "service.timeout_ms")]["chain"]
        self.assertTrue(chain["diagnostics"]["non_monotonic"])
        inversion = chain["diagnostics"]["bias_inversions"][0]
        self.assertEqual(inversion["basis"], "EXPLICIT_ORDER_OVERRIDES_BIAS")

    def test_same_input_and_parameters_are_byte_identical(self) -> None:
        first = Workspace()
        second = Workspace()
        try:
            report_one, code_one = run_diagnose(BASE, first.db, first.log)
            report_two, code_two = run_diagnose(BASE, second.db, second.log)
            self.assertEqual(code_one, 0)
            self.assertEqual(code_two, 0)
            self.assertEqual(canonical_dumps(report_one), canonical_dumps(report_two))
        finally:
            first.close()
            second.close()


class PersistenceAndReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Workspace()

    def tearDown(self) -> None:
        self.workspace.close()

    def _diagnose_base(self) -> dict:
        report, code = run_diagnose(
            BASE, self.workspace.db, self.workspace.log
        )
        self.assertEqual(code, 0)
        return report

    def _diagnose_rebiased(self) -> dict:
        report, code = run_diagnose(
            REBIASED, self.workspace.db, self.workspace.log
        )
        self.assertEqual(code, 0)
        return report

    def test_bias_change_increments_version_stales_old_and_replaces_view(self) -> None:
        first = self._diagnose_base()
        second = self._diagnose_rebiased()
        self.assertEqual(first["rule_version"], 1)
        self.assertEqual(second["rule_version"], 2)
        self.assertEqual(second["replacement"]["strategy"], "whole-view-replace")
        changes = {
            (change["environment_id"], change["key"]): change
            for change in second["replacement"]["changes"]
        }
        mode_change = changes[("prod", "service.mode")]
        self.assertEqual(mode_change["old_selected_sources"], ["file_override"])
        self.assertEqual(mode_change["new_selected_sources"], ["runtime_injection"])

        connection = sqlite3.connect(self.workspace.db)
        try:
            stale = connection.execute(
                "SELECT rule_version, stale, COUNT(*) "
                "FROM session_decisions GROUP BY rule_version, stale "
                "ORDER BY rule_version, stale"
            ).fetchall()
            current = connection.execute(
                "SELECT DISTINCT rule_version FROM current_decisions"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(stale, [(1, 1, 10), (2, 0, 10)])
        self.assertEqual(current, [(2,)])

    def test_reverting_to_old_rule_fingerprint_still_gets_new_version(self) -> None:
        first = self._diagnose_base()
        second = self._diagnose_rebiased()
        third, code = run_diagnose(
            BASE, self.workspace.db, self.workspace.log
        )
        self.assertEqual(code, 0)
        self.assertEqual(third["rule_version"], 3)
        self.assertNotEqual(first["session_id"], third["session_id"])
        self.assertEqual(
            third["rule_fingerprint"], first["rule_fingerprint"]
        )
        self.assertNotEqual(second["rule_version"], third["rule_version"])

    def test_verify_tool_checks_chain_selected_and_coverage(self) -> None:
        report = self._diagnose_base()
        verification = verify_session(
            self.workspace.db, report["session_id"]
        )
        self.assertEqual(verification["status"], "PASS")
        self.assertEqual(verification["summary"]["keys"], 10)
        self.assertGreaterEqual(verification["summary"]["checks"], 50)

    def test_restore_old_snapshot_replays_and_replaces_current_view(self) -> None:
        first = self._diagnose_base()
        second = self._diagnose_rebiased()
        restore, code = run_restore(
            self.workspace.db, first["session_id"], self.workspace.log
        )
        self.assertEqual(code, 0)
        self.assertEqual(restore["business_code"], "OK")
        self.assertEqual(restore["replay"]["status"], "PASS")
        self.assertTrue(restore["version_mismatch"])
        self.assertEqual(restore["snapshot_rule_version"], 1)
        self.assertEqual(restore["latest_rule_version"], 2)
        self.assertEqual(restore["replacement"]["strategy"], "whole-view-replace")

        connection = sqlite3.connect(self.workspace.db)
        try:
            current_versions = connection.execute(
                "SELECT DISTINCT rule_version FROM current_decisions"
            ).fetchall()
            stale_v2 = connection.execute(
                "SELECT DISTINCT stale FROM session_decisions WHERE rule_version = 2"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(current_versions, [(1,)])
        self.assertEqual(stale_v2, [(1,)])
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_tampered_snapshot_reports_e_replay_with_source(self) -> None:
        report = self._diagnose_base()
        connection = sqlite3.connect(self.workspace.db)
        try:
            row = connection.execute(
                "SELECT snapshot_json FROM sessions WHERE session_id = ?",
                (report["session_id"],),
            ).fetchone()
            snapshot = json.loads(row[0])
            for environment in snapshot["result"]["environments"]:
                if environment["environment_id"] == "prod":
                    for key in environment["keys"]:
                        if key["key"] == "service.mode":
                            key["decision"]["selected_source"] = "local_baseline"
                            break
            connection.execute(
                "UPDATE sessions SET snapshot_json = ? WHERE session_id = ?",
                (canonical_dumps(snapshot), report["session_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        replay, code = run_restore(
            self.workspace.db, report["session_id"], self.workspace.log
        )
        self.assertEqual(code, 4)
        self.assertEqual(replay["business_code"], "E_REPLAY")
        self.assertTrue(replay["errors"])
        self.assertEqual(replay["errors"][0]["environment_id"], "prod")
        self.assertEqual(replay["errors"][0]["config_key"], "service.mode")
        self.assertIsNotNone(replay["errors"][0]["source_id"])

    def test_verifier_detects_tampered_chain(self) -> None:
        report = self._diagnose_base()
        connection = sqlite3.connect(self.workspace.db)
        try:
            row = connection.execute(
                "SELECT chain_json FROM session_chains "
                "WHERE session_id = ? AND environment_id = 'prod' "
                "AND config_key = 'service.mode'",
                (report["session_id"],),
            ).fetchone()
            chain = json.loads(row[0])
            chain["digest"] = "0" * 64
            connection.execute(
                "UPDATE session_chains SET chain_json = ?, digest = ? "
                "WHERE session_id = ? AND environment_id = 'prod' "
                "AND config_key = 'service.mode'",
                (
                    canonical_dumps(chain),
                    "0" * 64,
                    report["session_id"],
                ),
            )
            connection.commit()
        finally:
            connection.close()
        verification = verify_session(self.workspace.db, report["session_id"])
        self.assertEqual(verification["status"], "FAIL")
        self.assertTrue(
            any(
                error["check"] == "chain_digest_recompute"
                for error in verification["errors"]
            )
        )


class InjectionTests(unittest.TestCase):
    def test_three_injections_emit_expected_business_codes_and_logs(self) -> None:
        expectations = {
            "env-corrupt": (E_ENV, 2),
            "binding-missing": (E_BIND, 2),
            "chain-digest": (E_CHAIN, 3),
        }
        for injection, (expected_code, expected_exit) in expectations.items():
            with self.subTest(injection=injection):
                workspace = Workspace()
                try:
                    report, code = run_diagnose(
                        BASE,
                        workspace.db,
                        workspace.log,
                        injection=injection,
                    )
                    self.assertEqual(code, expected_exit)
                    actual_code = report.get("business_code") or report.get(
                        "overall_code"
                    )
                    self.assertEqual(actual_code, expected_code)
                    logged_codes = [
                        json.loads(line)["business_code"]
                        for line in workspace.log.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    ]
                    self.assertIn(expected_code, logged_codes)
                finally:
                    workspace.close()

    def test_injection_is_parameterized_not_implementation_triggered(self) -> None:
        raw = json.loads(BASE.read_text(encoding="utf-8"))
        mutated = apply_injection(raw, "binding-missing")
        self.assertNotEqual(canonical_dumps(raw), canonical_dumps(mutated))
        self.assertEqual(raw, json.loads(BASE.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
