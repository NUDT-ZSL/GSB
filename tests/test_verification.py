from __future__ import annotations

import unittest

import numpy as np

from ddp_ckpt_lab.checkpoints import CheckpointStore
from ddp_ckpt_lab.errors import BusinessError
from ddp_ckpt_lab.training import Trainer, initialize_training
from ddp_ckpt_lab.verification import (
    VerificationService,
    assert_bitwise_equal,
    first_divergence,
)

from tests.helpers import TempWorkspace, make_config


class VerificationTests(unittest.TestCase):
    def _checkpoint(self, workspace):
        config = make_config()
        bundle = initialize_training(config)
        trainer = Trainer(config, bundle.dataset, bundle.state)
        trainer.train_until(2)
        store = CheckpointStore(workspace / "checkpoints")
        info = store.save(trainer.state, config)
        return config, store, info, trainer.state

    def test_resume_training_is_bitwise_identical(self) -> None:
        with TempWorkspace() as workspace:
            config, store, info, _ = self._checkpoint(workspace)
            report = VerificationService(store).verify_resume_consistency(
                config, info, 5
            )
            self.assertEqual(report["status"], "OK")
            self.assertTrue(report["resume_consistency"])
            self.assertEqual(report["final_step"], 7)
            self.assertEqual(
                report["final_baseline_state_digest"],
                report["final_resumed_state_digest"],
            )
            self.assertEqual(len(report["comparisons"]), 5)

    def test_first_divergence_and_e_diverge_are_precise(self) -> None:
        config = make_config()
        bundle = initialize_training(config)
        trainer = Trainer(config, bundle.dataset, bundle.state)
        trainer.train_until(3)
        expected = trainer.state.clone()
        actual = trainer.state.clone()
        value = actual.workers[2].momentum["W2"].reshape(-1)[0]
        actual.workers[2].momentum["W2"].reshape(-1)[0] = np.nextafter(value, np.inf)
        divergence = first_divergence(expected, actual, step=3)
        self.assertIsNotNone(divergence)
        assert divergence is not None
        self.assertEqual(divergence.worker_id, 2)
        self.assertEqual(divergence.parameter_name, "W2")
        self.assertEqual(divergence.state_kind, "momentum")
        with self.assertRaises(BusinessError) as context:
            assert_bitwise_equal(expected, actual, step=3)
        self.assertEqual(context.exception.code, "E_DIVERGE")
        location = context.exception.details["first_divergence"]
        self.assertEqual(location["worker_id"], 2)
        self.assertEqual(location["parameter_name"], "W2")
        self.assertEqual(location["step"], 3)

    def test_replay_is_reproducible_and_does_not_modify_checkpoint(self) -> None:
        with TempWorkspace() as workspace:
            config, store, info, _ = self._checkpoint(workspace)
            fingerprint_before = store.fingerprint(info)
            report = VerificationService(store).verify_replay(config, info, 4)
            fingerprint_after = store.fingerprint(info)
            self.assertTrue(report["reproducible"])
            self.assertEqual(report["first_run_state_digest"], report["second_run_state_digest"])
            self.assertEqual(report["checkpoint_fingerprint_before"], fingerprint_before)
            self.assertEqual(report["checkpoint_fingerprint_after"], fingerprint_before)
            self.assertEqual(fingerprint_after, fingerprint_before)
            self.assertTrue(report["checkpoint_unmodified"])

    def test_replay_with_sharding_change_is_e_replay(self) -> None:
        with TempWorkspace() as workspace:
            config, store, info, _ = self._checkpoint(workspace)
            requested = config.with_changes(sharding="contiguous")
            with self.assertRaises(BusinessError) as context:
                VerificationService(store).verify_replay(requested, info, 2)
            self.assertEqual(context.exception.code, "E_REPLAY")
            self.assertEqual(
                context.exception.details["reason"],
                "configuration_or_sharding_conflict",
            )
            self.assertEqual(
                context.exception.details["cause"]["code"], "E_CONFIG"
            )

    def test_resume_with_learning_rate_change_is_e_config(self) -> None:
        with TempWorkspace() as workspace:
            config, store, info, _ = self._checkpoint(workspace)
            requested = config.with_changes(learning_rate=80)
            with self.assertRaises(BusinessError) as context:
                VerificationService(store).verify_resume_consistency(
                    requested, info, 2
                )
            self.assertEqual(context.exception.code, "E_CONFIG")
            self.assertEqual(context.exception.details["field"], "learning_rate")


if __name__ == "__main__":
    unittest.main()
