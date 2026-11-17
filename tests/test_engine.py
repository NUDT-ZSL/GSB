from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ticket_risk.commands import parse_command
from ticket_risk.engine import InteractiveEngine
from ticket_risk.errors import (
    E_CONFIG_MISMATCH,
    E_INCONSISTENT,
    E_INTERACT_BUDGET_PENDING,
    E_SNAPSHOT_INVALID,
    SnapshotError,
)
from ticket_risk.loader import load_inputs
from ticket_risk.pipeline import run_full_serial
from ticket_risk.util import sha256_obj


ROOT = Path(__file__).resolve().parents[1]


def make_bundle(
    *,
    config_mutator=None,
    rules_mutator=None,
    sample_limit: int | None = None,
):
    bundle = load_inputs(
        ROOT / "data/samples.jsonl",
        ROOT / "resources/rules.json",
        ROOT / "config/determination.json",
    )
    if config_mutator:
        config_mutator(bundle.config)
    if rules_mutator:
        rules_mutator(bundle.rules)
    if sample_limit is not None:
        bundle.samples = bundle.samples[:sample_limit]
    bundle.config_hash = sha256_obj(bundle.config)
    bundle.resource_fingerprint = sha256_obj(bundle.rules)
    return bundle


def full_from_state(engine: InteractiveEngine):
    state = engine.state_snapshot
    loader_failures = [failure for failure in state.failures if failure.stage is None]
    return run_full_serial(state.samples, state.config, state.rules, loader_failures)


COMMANDS = [
    "set_lexicon 满意 0.95",
    "set_threshold 风险 0.62",
    "set_weight fusion_temperature 0.42",
    "set_risk_keyword 投诉 0.82",
    "set_lexicon 失望 -0.95",
    "set_degree 非常 1.90",
    "set_negation_window 5",
    "set_risk_level_weight 催办 0.60",
    "set_weight position_decay 0.16",
]


class EngineTests(unittest.TestCase):
    def test_each_command_matches_full_serial(self) -> None:
        bundle = make_bundle(
            config_mutator=lambda config: config.update(
                interaction_budget_ms=0.0,
                snapshot_commit_interval=1000,
                worker_threads=4,
            ),
            sample_limit=120,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = InteractiveEngine(
                bundle,
                Path(directory) / "snapshots",
                Path(directory) / "report.json",
                fresh=True,
            )
            try:
                for raw in COMMANDS:
                    response = engine.apply_edit(parse_command(raw))
                    self.assertIn(E_INTERACT_BUDGET_PENDING, response["business_codes"])
                    self.assertTrue(engine.wait_idle(30.0))
                    incremental = engine.state_snapshot.canonical_payload()
                    full = full_from_state(engine).canonical_payload()
                    self.assertEqual(incremental, full, msg=raw)
                self.assertTrue(engine.verify_consistency()["consistent"])
            finally:
                engine.graceful_shutdown()

    def test_targeted_edit_reuses_unreferenced_samples(self) -> None:
        bundle = make_bundle(
            config_mutator=lambda config: config.update(
                interaction_budget_ms=100.0,
                snapshot_commit_interval=1000,
            ),
            sample_limit=120,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = InteractiveEngine(
                bundle,
                Path(directory) / "snapshots",
                Path(directory) / "report.json",
                fresh=True,
            )
            try:
                response = engine.apply_edit(parse_command("set_lexicon 满意 0.91"))
                self.assertTrue(engine.wait_idle(30.0))
                state = engine.state_snapshot
                audit = state.last_audit
                self.assertLess(audit["affected_sample_ids"].__len__(), len(state.samples))
                self.assertGreater(audit["reused_sample_count"], 0)
                self.assertFalse(
                    set(audit["recomputed_sample_ids"])
                    - set(audit["affected_sample_ids"])
                )
                self.assertTrue(engine.verify_consistency()["consistent"])
            finally:
                engine.graceful_shutdown()

    def test_budgeted_response_is_pending_before_atomic_commit(self) -> None:
        bundle = make_bundle(
            config_mutator=lambda config: config.update(
                interaction_budget_ms=0.0,
                snapshot_commit_interval=1000,
            ),
            sample_limit=300,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = InteractiveEngine(
                bundle,
                Path(directory) / "snapshots",
                Path(directory) / "report.json",
                fresh=True,
            )
            try:
                response = engine.apply_edit(parse_command("set_threshold 风险 0.61"))
                self.assertTrue(response["pending"])
                self.assertEqual(response["latest_completed_version"], 0)
                self.assertIn(E_INTERACT_BUDGET_PENDING, response["business_codes"])
                self.assertEqual(response["summary"]["state_version"], 0)
                self.assertEqual(response["pending_progress"]["target_version"], 1)
                self.assertTrue(engine.wait_idle(30.0))
                self.assertEqual(engine.state_snapshot.version, 1)
            finally:
                engine.graceful_shutdown()

    def test_different_worker_counts_produce_same_payload(self) -> None:
        payloads = []
        for workers in (1, 8):
            bundle = make_bundle(
                config_mutator=lambda config, workers=workers: config.update(
                    worker_threads=workers,
                    interaction_budget_ms=0.0,
                    snapshot_commit_interval=1000,
                ),
                sample_limit=100,
            )
            with tempfile.TemporaryDirectory() as directory:
                engine = InteractiveEngine(
                    bundle,
                    Path(directory) / "snapshots",
                    Path(directory) / "report.json",
                    fresh=True,
                )
                try:
                    for raw in COMMANDS[:5]:
                        engine.apply_edit(parse_command(raw))
                        self.assertTrue(engine.wait_idle(30.0))
                    payloads.append(engine.state_snapshot.canonical_payload())
                finally:
                    engine.graceful_shutdown()
        self.assertEqual(payloads[0], payloads[1])

    def test_snapshot_restore_and_config_mismatch(self) -> None:
        bundle = make_bundle(
            config_mutator=lambda config: config.update(
                interaction_budget_ms=100.0,
                snapshot_commit_interval=1,
            ),
            sample_limit=80,
        )
        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory) / "snapshots"
            report_path = Path(directory) / "report.json"
            first = InteractiveEngine(bundle, snapshot_dir, report_path, fresh=True)
            first.apply_edit(parse_command("set_lexicon 满意 0.93"))
            first.wait_idle(30.0)
            expected = first.state_snapshot.canonical_payload()
            expected_version = first.state_snapshot.version
            first.graceful_shutdown()

            second_bundle = make_bundle(
                config_mutator=lambda config: config.update(
                    interaction_budget_ms=100.0,
                    snapshot_commit_interval=1,
                ),
                sample_limit=80,
            )
            second = InteractiveEngine(second_bundle, snapshot_dir, report_path)
            self.assertTrue(second.recovery_info["recovered"])
            self.assertEqual(second.state_snapshot.version, expected_version)
            self.assertEqual(second.state_snapshot.canonical_payload(), expected)
            second.graceful_shutdown()

            mismatch_bundle = make_bundle(
                config_mutator=lambda config: config.update(
                    interaction_budget_ms=101.0,
                    snapshot_commit_interval=1,
                ),
                sample_limit=80,
            )
            with self.assertRaises(SnapshotError) as raised:
                InteractiveEngine(mismatch_bundle, snapshot_dir, report_path)
            self.assertEqual(raised.exception.code, E_CONFIG_MISMATCH)

    def test_corrupt_stage_snapshot_is_rejected(self) -> None:
        bundle = make_bundle(sample_limit=20)
        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory) / "snapshots"
            report_path = Path(directory) / "report.json"
            engine = InteractiveEngine(bundle, snapshot_dir, report_path, fresh=True)
            engine.graceful_shutdown()
            manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
            stage_path = snapshot_dir / manifest["stage_files"]["sentiment"]["file"]
            stage_path.write_text(stage_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaises(SnapshotError) as raised:
                InteractiveEngine(bundle, snapshot_dir, report_path)
            self.assertEqual(raised.exception.code, E_SNAPSHOT_INVALID)

    def test_stage_exception_is_isolated(self) -> None:
        bundle = make_bundle(sample_limit=24)
        original = __import__("ticket_risk.pipeline", fromlist=["sentiment_stage"]).sentiment_stage
        target_id = bundle.samples[7].sample_id

        def flaky(sample, *args, **kwargs):
            if sample.sample_id == target_id:
                raise RuntimeError("injected stage fault")
            return original(sample, *args, **kwargs)

        with mock.patch("ticket_risk.pipeline.sentiment_stage", side_effect=flaky):
            state = run_full_serial(bundle.samples, bundle.config, bundle.rules, bundle.failures)
        self.assertEqual(len(state.results()), len(bundle.samples) - 1)
        failure = next(item for item in state.failures if item.sample_id == target_id)
        self.assertEqual(failure.business_code, "E_STAGE_EXCEPTION")
        self.assertEqual(failure.stage, "sentiment")
        self.assertEqual(failure.exception_type, "RuntimeError")

    def test_verify_reports_inconsistent_field(self) -> None:
        bundle = make_bundle(
            config_mutator=lambda config: config.update(snapshot_commit_interval=1000),
            sample_limit=16,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = InteractiveEngine(
                bundle,
                Path(directory) / "snapshots",
                Path(directory) / "report.json",
                fresh=True,
            )
            try:
                sample_id = engine.state_snapshot.samples[0].sample_id
                engine.state_snapshot.artifacts[sample_id]["fusion"]["confidence"] = 0.123456
                result = engine.verify_consistency()
                self.assertFalse(result["consistent"])
                self.assertIn(E_INCONSISTENT, result["business_codes"])
                self.assertTrue(result["differences"])
            finally:
                engine.graceful_shutdown()


if __name__ == "__main__":
    unittest.main()
