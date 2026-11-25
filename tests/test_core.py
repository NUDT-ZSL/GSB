from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from tii_app.budget import Budgets, validate_budget
from tii_app.configs import ResolvedConfig
from tii_app.errors import ELedgerCorrupt, EMemoryBudget, EStageBusy
from tii_app.ledger import CommitGuard, VersionedLedger
from tii_app.models import Task
from tii_app.ops import (
    build_model,
    execute_operator_chain,
    forward_chain,
    inference_chain,
    train_backward_chain,
)
from tii_app.sample_data import generate_sample, quantize_dequantize
from tii_app.util import atomic_write_json, sha256_obj


def make_task(**overrides) -> Task:
    values = {
        "case_id": "core-case",
        "data_size": 12,
        "feature_dim": 5,
        "label_dim": 3,
        "dtype": "int8",
        "quant_scale": 0.05,
        "quant_zero_point": 0,
        "block_plan": {"row_block": 4, "col_block": 2, "priority": 2},
        "revision": 1,
        "logical_clock_ms": 1500,
    }
    values.update(overrides)
    return Task.from_dict(values)


def make_config(generation: int = 2, effective_at_ms: int = 1500) -> ResolvedConfig:
    hyperparameters = {
        "seed_offset": 11,
        "weight_scale": 0.95,
        "learning_rate": 0.02,
        "activation": "tanh",
        "rounding": "half_away_from_zero",
    }
    return ResolvedConfig(
        generation=generation,
        effective_at_ms=effective_at_ms,
        hyperparameters=hyperparameters,
        config_hash=sha256_obj(
            {
                "generation": generation,
                "effective_at_ms": effective_at_ms,
                "hyperparameters": hyperparameters,
            }
        ),
        source="test",
        shard_indices=(0,),
    )


class SampleAndOperatorTests(unittest.TestCase):
    def test_deterministic_sample_and_quantization(self) -> None:
        task = make_task()
        first = generate_sample(task, 42)
        second = generate_sample(task, 42)
        self.assertEqual(first["data_digest"], second["data_digest"])
        self.assertEqual(first["features"].shape, (task.data_size, task.feature_dim))
        self.assertEqual(first["labels"].shape, (task.data_size, task.label_dim))
        self.assertTrue(np.all(first["labels"].sum(axis=1) == 1.0))
        quant = quantize_dequantize(first["features"], task)
        self.assertEqual(quant["quantized"].shape, first["features"].shape)
        self.assertEqual(str(quant["quantized"].dtype), task.dtype)
        self.assertLessEqual(quant["summary"]["max_abs_error"], task.quant_scale / 2.0)
        repeat = quantize_dequantize(first["features"], task)
        self.assertEqual(quant["quant_digest"], repeat["quant_digest"])

    def test_training_and_inference_share_forward_chain(self) -> None:
        task = make_task()
        config = make_config()
        sample = generate_sample(task, 42)
        quant = quantize_dequantize(sample["features"], task)
        model = build_model(task, config)
        forward = forward_chain(quant["dequantized"], task, model, config)
        train = train_backward_chain(forward, {**sample, "dequantized": quant["dequantized"]}, task, config)
        inference = inference_chain(forward, task, config)
        expected_rows = [[0, 4], [4, 8], [8, 12]]
        self.assertEqual(forward["row_boundaries"], expected_rows)
        self.assertEqual(
            train["summary"]["forward_digest"],
            inference["summary"]["forward_digest"],
        )
        self.assertEqual(
            inference["summary"]["forward_digest"],
            forward["summary"]["forward_digest"],
        )
        self.assertEqual(train["grad_weights"].shape, (task.feature_dim, task.label_dim))
        self.assertEqual(inference["predictions"].shape, (task.data_size,))

        model_again = build_model(task, config)
        forward_again = forward_chain(quant["dequantized"], task, model_again, config)
        self.assertEqual(
            model["summary"]["model_digest"],
            model_again["summary"]["model_digest"],
        )
        self.assertEqual(
            forward["summary"]["forward_digest"],
            forward_again["summary"]["forward_digest"],
        )
        unified = execute_operator_chain(
            task=task,
            config=config,
            sample=sample,
            dequantized=quant["dequantized"],
            mode="both",
            model=model,
        )
        self.assertEqual(
            unified["train"]["summary"]["forward_digest"],
            unified["inference"]["summary"]["forward_digest"],
        )
        self.assertTrue(unified["block_boundaries_shared"])

    def test_memory_budget_rejects_overlimit(self) -> None:
        task = make_task(data_size=64, feature_dim=8, label_dim=4)
        admitted = validate_budget(task, "both", Budgets())
        self.assertTrue(admitted["ok"])
        with self.assertRaises(EMemoryBudget) as raised:
            validate_budget(
                task,
                "both",
                Budgets(
                    total_feature_buffer_size=1,
                    single_pass_workspace_size=1,
                    model_workspace_size=1,
                ),
            )
        self.assertEqual(raised.exception.code, "E_MEMORY_BUDGET")


class LedgerTests(unittest.TestCase):
    def test_full_snapshot_replacement_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = VersionedLedger(Path(directory), "test")
            first = ledger.commit(
                {"value": 1},
                expected_version=None,
                expected_checksum=None,
            )
            version = first["version"]
            checksum = first["checksum"]
            with CommitGuard(ledger.lock_path):
                with self.assertRaises(EStageBusy) as raised:
                    ledger.commit(
                        {"value": 2},
                        expected_version=version,
                        expected_checksum=checksum,
                    )
            self.assertEqual(raised.exception.code, "E_STAGE_BUSY")
            self.assertEqual(ledger.read()["data"], {"value": 1})
            second = ledger.commit(
                {"value": 2},
                expected_version=version,
                expected_checksum=checksum,
            )
            self.assertEqual(second["version"], version + 1)
            self.assertEqual(ledger.read()["data"], {"value": 2})

    def test_concurrent_commits_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = VersionedLedger(Path(directory), "race")
            initial = ledger.commit(
                {"winner": None},
                expected_version=None,
                expected_checksum=None,
            )
            outcomes: list[str] = []
            barrier = threading.Barrier(3)

            def contender(name: str) -> None:
                barrier.wait()
                try:
                    ledger.commit(
                        {"winner": name},
                        expected_version=initial["version"],
                        expected_checksum=initial["checksum"],
                    )
                    outcomes.append(name)
                except EStageBusy:
                    outcomes.append("busy")

            threads = [
                threading.Thread(target=contender, args=(name,))
                for name in ("a", "b")
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(sum(item in {"a", "b"} for item in outcomes), 1)
            self.assertEqual(sum(item == "busy" for item in outcomes), 1)

    def test_checksum_tamper_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = VersionedLedger(Path(directory), "tamper")
            snapshot = ledger.commit(
                {"value": 1},
                expected_version=None,
                expected_checksum=None,
            )
            tampered = dict(snapshot)
            tampered["data"] = {"value": 99}
            atomic_write_json(ledger.path, tampered)
            with self.assertRaises(ELedgerCorrupt):
                ledger.read()


if __name__ == "__main__":
    unittest.main()
