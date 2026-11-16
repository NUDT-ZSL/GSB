from __future__ import annotations

import unittest

import numpy as np

from ddp_ckpt_lab.canonical import arrays_equal
from ddp_ckpt_lab.config import TrainingConfig
from ddp_ckpt_lab.data import generate_synthetic_dataset, initial_batch_plan_digest
from ddp_ckpt_lab.errors import BusinessError
from ddp_ckpt_lab.model import initial_parameters
from ddp_ckpt_lab.training import Trainer, initialize_training

from tests.helpers import make_config


class DeterminismTests(unittest.TestCase):
    def test_dataset_parameters_and_batch_plan_are_deterministic(self) -> None:
        config = make_config()
        first = initialize_training(config)
        second = initialize_training(config)
        self.assertEqual(first.data_digest, second.data_digest)
        self.assertEqual(first.parameter_digest, second.parameter_digest)
        self.assertEqual(first.batch_plan_digest, second.batch_plan_digest)
        self.assertEqual(first.state_digest, second.state_digest)
        self.assertTrue(
            arrays_equal(first.dataset.features, second.dataset.features)
        )
        self.assertTrue(arrays_equal(first.dataset.labels, second.dataset.labels))
        params_a = initial_parameters(config)
        params_b = initial_parameters(config)
        for name in params_a:
            self.assertTrue(arrays_equal(params_a[name], params_b[name]))

        dataset_a = generate_synthetic_dataset(config)
        dataset_b = generate_synthetic_dataset(config)
        self.assertEqual(dataset_a.digest, dataset_b.digest)
        self.assertEqual(
            initial_batch_plan_digest(config),
            initial_batch_plan_digest(config),
        )

    def test_two_training_runs_are_bitwise_identical(self) -> None:
        config = make_config()
        first_bundle = initialize_training(config)
        second_bundle = initialize_training(config)
        first = Trainer(config, first_bundle.dataset, first_bundle.state)
        second = Trainer(config, second_bundle.dataset, second_bundle.state)
        first_records = first.train_steps(8)
        second_records = second.train_steps(8)
        self.assertEqual(
            first.state.state_digest(config), second.state.state_digest(config)
        )
        self.assertEqual(first.state.payload_digest(), second.state.payload_digest())
        self.assertEqual(
            [record.state_digest for record in first_records],
            [record.state_digest for record in second_records],
        )
        self.assertEqual(
            [record.progress_digest for record in first_records],
            [record.progress_digest for record in second_records],
        )
        for left, right in zip(first_records, second_records):
            self.assertEqual(left.loss.hex(), right.loss.hex())
        for worker_a, worker_b in zip(first.state.workers, second.state.workers):
            for name in worker_a.parameters:
                self.assertTrue(
                    arrays_equal(worker_a.parameters[name], worker_b.parameters[name])
                )
                self.assertTrue(
                    arrays_equal(worker_a.momentum[name], worker_b.momentum[name])
                )

    def test_steps_and_progress_digests_strictly_advance(self) -> None:
        config = make_config()
        bundle = initialize_training(config)
        trainer = Trainer(config, bundle.dataset, bundle.state)
        records = trainer.train_steps(8)
        self.assertEqual([record.step for record in records], list(range(1, 9)))
        digests = [record.progress_digest for record in records]
        self.assertEqual(len(digests), len(set(digests)))
        self.assertNotEqual(digests[0], digests[1])

    def test_expected_digest_mismatch_is_e_init(self) -> None:
        config = make_config()
        with self.assertRaises(BusinessError) as context:
            initialize_training(
                config,
                expected_digests={"batch_plan_digest": "0" * 64},
            )
        self.assertEqual(context.exception.code, "E_INIT")
        self.assertEqual(
            context.exception.details["mismatches"][0]["name"],
            "batch_plan_digest",
        )

    def test_invalid_worker_count_is_e_config(self) -> None:
        with self.assertRaises(BusinessError) as context:
            TrainingConfig(
                seed=1,
                data_seed=1,
                samples=64,
                features=4,
                hidden_dim=6,
                workers=3,
                batch_size=8,
                train_steps=4,
                learning_rate=10,
                momentum=900,
                sharding="modulo",
                checkpoint_every=2,
            )
        self.assertEqual(context.exception.code, "E_CONFIG")


if __name__ == "__main__":
    unittest.main()
