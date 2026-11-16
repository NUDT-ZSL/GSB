from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from regularization_lab.config import load_experiment
from regularization_lab.fingerprint import config_hash
from regularization_lab.lab import ExperimentSession

from tests.helpers import ROOT


def short_config(config, root: Path, *, name: str):
    return replace(
        config,
        optimizer=replace(config.optimizer, max_iterations=6, tolerance=0.0),
        runtime=replace(
            config.runtime,
            state_dir=root / name,
            report_path=root / f"{name}.json",
        ),
        regularizers=[config.regularizers[0]],
    )


class InvalidationAndInteractionTests(unittest.TestCase):
    def test_changed_regularization_hash_refuses_snapshot_restore(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = short_config(base, root, name="state")
            paused_session = ExperimentSession(original)
            paused = paused_session.run_scheme("ridge", pause_after_round=3)
            assert paused is not None
            self.assertEqual(paused["status"], "interrupted")

            changed = replace(
                original,
                regularizers=[replace(original.regularizers[0], alpha=0.09)],
            )
            changed_session = ExperimentSession(changed)
            self.assertTrue(
                any(item["business_code"] == "E_CONFIG_MISMATCH" for item in changed_session.failures)
            )
            assert changed_session.dataset is not None
            changed_hash = config_hash(
                changed,
                changed_session.dataset,
                changed_session.schemes["ridge"],
            )
            snapshot, restore_failures = changed_session.snapshot_store.load_latest(
                config_hash=changed_hash,
                config_schema_version=changed.schema_version,
                known_config_hashes=changed_session.known_config_hashes(),
            )
            self.assertIsNone(snapshot)
            self.assertTrue(
                any(item.business_code == "E_CONFIG_MISMATCH" for item in restore_failures)
            )
            result = changed_session.run_scheme("ridge")
            assert result is not None
            self.assertIsNone(result["resumed_from_round"])
            report = changed_session.write_report()
            self.assertIn("E_CONFIG_MISMATCH", report["business_codes"])

    def test_penalty_change_invalidates_then_original_cache_is_reused(self) -> None:
        base = load_experiment(ROOT / "configs" / "experiment.yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = short_config(base, root, name="interactive")
            config = replace(
                config,
                optimizer=replace(config.optimizer, tolerance=1.0e9, max_iterations=4),
            )
            session = ExperimentSession(config)
            first = session.run_scheme("ridge")
            assert first is not None
            first_hash = first["config_hash"]
            first_weights = first["final_coefficients"]["weights"]
            snapshots_after_first = len(list((root / "interactive" / "snapshots").glob("*/*.json")))

            self.assertTrue(session.set_penalty(0.09))
            self.assertFalse(any(
                decision.get("cache_probe", {}).get("hit")
                for decision in session.decisions
                if decision.get("action") == "set_penalty"
            ))
            changed = session.run_scheme("ridge")
            assert changed is not None
            self.assertNotEqual(changed["config_hash"], first_hash)
            self.assertTrue(changed["cache_decision"]["training_executed"])

            self.assertTrue(session.set_penalty(0.08))
            replayed = session.run_scheme("ridge")
            assert replayed is not None
            self.assertTrue(replayed["cache_reused"])
            self.assertEqual(replayed["training_executed_rounds"], [])
            self.assertEqual(replayed["cache_decision"]["recomputed_rounds"], [])
            self.assertEqual(replayed["final_coefficients"]["weights"], first_weights)
            snapshots_after_replay = len(list((root / "interactive" / "snapshots").glob("*/*.json")))
            self.assertEqual(snapshots_after_replay, snapshots_after_first + changed["iterations_completed"])

            report = session.write_report()
            self.assertTrue(report["reuse_and_recompute"]["ridge"]["cache_hit"])
            actions = [item["action"] for item in report["decision_log"]]
            self.assertIn("set_penalty", actions)
            self.assertIn("run_scheme", actions)


if __name__ == "__main__":
    unittest.main()
