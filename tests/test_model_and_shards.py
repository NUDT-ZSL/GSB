from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shardcheck.config import PolicyRule, SampleBundle, TrainingConfig
from shardcheck.model import DeterministicModel
from shardcheck.shards import (
    AtomicShardWriter,
    build_logical_shards,
    make_physical_shard,
    plan_merge_groups,
    read_generation_payload,
)


ROOT = Path(__file__).resolve().parents[1]


class ModelAndShardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle = SampleBundle.load(ROOT / "samples")

    def test_fixed_point_training_is_repeatable(self) -> None:
        left = DeterministicModel(self.bundle.config)
        right = DeterministicModel(self.bundle.config)
        self.assertEqual(left.digest, right.digest)
        for round_number in range(1, 4):
            left_report = left.apply_round(round_number, self.bundle.data_shards)
            right_report = right.apply_round(round_number, self.bundle.data_shards)
            self.assertEqual(left.digest, right.digest)
            self.assertEqual(left_report, right_report)
        replay = DeterministicModel(self.bundle.config)
        for round_number in range(1, 4):
            replay.apply_round(round_number, self.bundle.data_shards)
        self.assertEqual(left.to_bytes(), replay.to_bytes())

    def test_merge_and_split_are_lossless(self) -> None:
        model = DeterministicModel(self.bundle.config)
        logical = build_logical_shards(model, 1)
        rule = PolicyRule(
            rule_id="merge-test",
            occupancy_min=0.0,
            occupancy_max=1.0,
            remaining_rounds_min=0,
            remaining_rounds_max=100,
            retention_generations=2,
            merge_mode="merge_small",
            merge_small_threshold_bytes=2500,
            max_merged_bytes=4096,
            write_priority="tensor_size_desc",
            budget_action="evict_then_write",
        )
        groups = plan_merge_groups(logical, rule)
        physicals = []
        merge_operations = []
        split_operations = []
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writer = AtomicShardWriter(root / "shards")
            for group_index, group in enumerate(groups):
                physical, merge_op, split_op = make_physical_shard(group, 1, group_index)
                physicals.append(physical)
                writer.write(physical)
                if merge_op:
                    merge_operations.append(merge_op)
                if split_op:
                    split_operations.append(split_op)
            generation = {
                "shards": [physical.descriptor for physical in physicals],
            }
            payload, restored_logical, observed_splits = read_generation_payload(root, generation)
            self.assertEqual(payload, model.to_bytes())
            self.assertEqual(len(restored_logical), len(logical))
            self.assertEqual(len(observed_splits), len(split_operations))
            self.assertTrue(all(operation["lossless"] for operation in split_operations))
            self.assertGreater(len(merge_operations), 0)

    def test_atomic_writer_is_idempotent_and_rejects_conflicts(self) -> None:
        model = DeterministicModel(self.bundle.config)
        logical = build_logical_shards(model, 1)
        physical, _, _ = make_physical_shard([logical[0]], 1, 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            writer = AtomicShardWriter(Path(temp_dir))
            first = writer.write(physical)
            second = writer.write(physical)
            self.assertEqual(first["status"], "committed")
            self.assertEqual(second["status"], "idempotent_skip")
            original = (Path(temp_dir) / f"{physical.shard_id}.bin").read_bytes()
            mutated = bytes([physical.payload[0] ^ 0xFF]) + physical.payload[1:]
            with self.assertRaises(Exception) as caught:
                writer.write(physical, payload_override=mutated)
            self.assertEqual(getattr(caught.exception, "code", None), "E_SHARD_CONFLICT")
            self.assertEqual((Path(temp_dir) / f"{physical.shard_id}.bin").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
