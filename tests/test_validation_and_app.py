from __future__ import annotations

import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from support_summary.errors import BusinessError
from support_summary.recovery import run_recovery_demo
from support_summary.sample_data import add_session_record, remove_session_record
from support_summary.validation import (
    load_config,
    load_sessions,
    load_vocabulary,
    validate_config,
    validate_sessions,
)

from .helpers import make_dataset


class ValidationAndAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = make_dataset(self.root)
        self.sessions = load_sessions(self.paths["sessions"])
        self.vocabulary = load_vocabulary(self.paths["vocabulary"])
        self.config = load_config(self.paths["config"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_configuration_and_input_error_codes(self) -> None:
        cases = [
            (
                replace(self.config, precision="fp8"),
                "E_PRECISION_INVALID",
                "precision",
            ),
            (
                replace(self.config, block_size=0),
                "E_BLOCK_SIZE_INVALID",
                "block_size",
            ),
            (
                replace(self.config, rewrite_strength=1.5),
                "E_STRENGTH_INVALID",
                "rewrite_strength",
            ),
        ]
        for config, code, field in cases:
            with self.assertRaises(BusinessError) as caught:
                validate_config(config)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.field, field)

        bad_weights = dict(self.config.weights)
        bad_weights["length"] = -0.1
        with self.assertRaises(BusinessError) as caught:
            validate_config(replace(self.config, weights=bad_weights))
        self.assertEqual(caught.exception.code, "E_WEIGHT_INVALID")
        self.assertIn("length", caught.exception.field)

    def test_duplicate_session_is_rejected(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            validate_sessions([self.sessions[0], self.sessions[0]])
        self.assertEqual(caught.exception.code, "E_DUPLICATE_SESSION")
        self.assertEqual(
            caught.exception.session_id, self.sessions[0].session_id
        )

    def test_missing_vocabulary_reports_session_and_field(self) -> None:
        from support_summary.pipeline import PipelineRunner

        bad_session = replace(
            self.sessions[0],
            turns=self.sessions[0].turns
            + (
                replace(
                    self.sessions[0].turns[0],
                    turn_index=max(
                        turn.turn_index for turn in self.sessions[0].turns
                    )
                    + 1,
                    text="ZZZ",
                ),
            ),
        )
        runner = PipelineRunner(
            [bad_session],
            self.vocabulary,
            self.config,
            cache_dir=self.root / "cache-vocab-missing",
        )
        with self.assertRaises(BusinessError) as caught:
            runner.run()
        self.assertEqual(caught.exception.code, "E_VOCAB_MISSING")
        self.assertEqual(caught.exception.session_id, bad_session.session_id)
        self.assertIn("turns[", caught.exception.field)

    def test_resource_limit_error_and_degrade(self) -> None:
        from support_summary.pipeline import PipelineRunner

        limited = replace(self.config, max_sessions_per_stage=1)
        runner = PipelineRunner(
            self.sessions,
            self.vocabulary,
            limited,
            cache_dir=self.root / "cache-limit",
        )
        with self.assertRaises(BusinessError) as caught:
            runner.run()
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            caught.exception.field, "max_sessions_per_stage"
        )

        degraded = replace(limited, resource_policy="degrade")
        result = PipelineRunner(
            self.sessions,
            self.vocabulary,
            degraded,
            cache_dir=self.root / "cache-degrade",
        ).run()
        self.assertEqual(
            result.degradations[0]["category"], "session_count"
        )

    def test_session_editing_and_duplicate_guard(self) -> None:
        before = len(load_sessions(self.paths["sessions"]))
        add_session_record(self.paths["sessions"], "S900")
        self.assertEqual(
            len(load_sessions(self.paths["sessions"])), before + 1
        )
        with self.assertRaises(BusinessError) as caught:
            add_session_record(self.paths["sessions"], "S900")
        self.assertEqual(caught.exception.code, "E_DUPLICATE_SESSION")
        remove_session_record(self.paths["sessions"], "S900")
        self.assertEqual(
            len(load_sessions(self.paths["sessions"])), before
        )

    def test_cli_config_set_rejects_invalid_without_writing(self) -> None:
        command = [
            sys.executable,
            "-m",
            "support_summary",
            "config",
            "set",
            "--root",
            str(self.root),
            "precision",
            "fp8",
        ]
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["error"]["code"], "E_PRECISION_INVALID")
        self.assertEqual(load_config(self.paths["config"]).precision, "fp32")

    def test_forced_termination_recovery_demo(self) -> None:
        report = run_recovery_demo(self.root)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["forced_exit_code"], 97)
        self.assertTrue(report["byte_identical"])
        self.assertEqual(
            report["resumed_stages"], ["chunk_tokenize", "unit_score"]
        )
        self.assertEqual(
            report["recomputed_stages"], ["rewrite", "summary_merge"]
        )


if __name__ == "__main__":
    unittest.main()
