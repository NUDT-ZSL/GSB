from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sample_generator import generate
from tii_app.configs import (
    ConfigStore,
    load_v2_snapshot,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    normalize_v1_document,
    write_v2_snapshot,
)
from tii_app.invalidation import (
    build_both_scenario,
    build_config_only_scenario,
    build_data_only_scenario,
)
from tii_app.models import load_tasks


class MigrationTests(unittest.TestCase):
    def test_missing_v1_fields_are_defaulted_with_provenance(self) -> None:
        sparse = {
            "config_version": 1,
            "effective_relative_seconds": 0.5,
            "hyperparameters": {"weight_scale": 1.2, "activation": "tanh"},
        }
        normalized, sources, filled = normalize_v1_document(sparse)
        self.assertEqual(normalized["hyperparameters"]["weight_scale"], 1.2)
        self.assertIn("seed_offset", normalized["hyperparameters"])
        self.assertIn("default:", sources["hyperparameters.seed_offset"])
        self.assertTrue(
            any(item["field"] == "hyperparameters.seed_offset" for item in filled)
        )
        migrated = migrate_v1_to_v2(sparse, base_epoch_ms=1000, target_generation=2)
        self.assertEqual(migrated["effective_at_ms"], 1500)
        self.assertTrue(migrated["field_sources"])
        self.assertFalse(migrated["dropped_fields"])

    def test_v2_to_v1_drops_generation_shards_and_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            v2_dir = Path(directory) / "v2"
            write_v2_snapshot(
                v2_dir,
                generations=[
                    {
                        "config_generation": 2,
                        "effective_at_ms": 1500,
                        "shards": [
                            {"hyperparameters": {"weight_scale": 0.9, "activation": "tanh"}},
                            {"hyperparameters": {"seed_offset": 11}},
                        ],
                    }
                ],
                tombstones=[
                    {
                        "tombstone_id": "t1",
                        "config_generation": 2,
                        "field_path": "hyperparameters.legacy",
                        "effective_at_ms": 1500,
                        "reason": "removed",
                    }
                ],
            )
            snapshot = load_v2_snapshot(v2_dir)
            self.assertEqual(len(snapshot["generations"][0]["shards"]), 2)
            migration = migrate_v2_to_v1(v2_dir)
            dropped_names = {item["field"] for item in migration["dropped_fields"]}
            self.assertIn("config_generation", dropped_names)
            self.assertIn("shard_indices", dropped_names)
            self.assertIn("tombstone:hyperparameters.legacy", dropped_names)
            self.assertEqual(migration["v1"]["hyperparameters"]["weight_scale"], 0.9)


class InvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.input_dir = root / "input"
        generate(self.input_dir, root / "out", 42)
        self.tasks = load_tasks(self.input_dir).tasks
        self.store = ConfigStore(
            self.input_dir / "configs" / "config_v1.json",
            self.input_dir / "configs" / "v2",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _current_config(self, task):
        return self.store.resolve_at(task.logical_clock_ms)

    def test_config_only_reuses_data_upstream(self) -> None:
        plan = build_config_only_scenario(
            self.tasks,
            v1_config=self.store.v1_resolved,
            current_config_for=self._current_config,
            global_seed=42,
        )
        coverage = plan["coverage"]
        self.assertTrue(coverage["disjoint"])
        self.assertTrue(coverage["covers_all"])
        for record in plan["tasks"]:
            if record["classification"] == "config_only":
                self.assertTrue({"sample", "quantize"}.issubset(record["reused_stages"]))
                self.assertIn("forward", record["recomputed_stages"])

    def test_data_only_reuses_model_and_rebuilds_sample(self) -> None:
        plan = build_data_only_scenario(
            self.tasks,
            revision_bumps={"case-003"},
            size_changes={"case-008": 104},
            config_for=self._current_config,
            global_seed=42,
        )
        self.assertTrue(plan["coverage"]["disjoint"])
        self.assertTrue(plan["coverage"]["covers_all"])
        for record in plan["tasks"]:
            if record["classification"] == "data_only":
                self.assertIn("model", record["reused_stages"])
                self.assertIn("sample", record["recomputed_stages"])

    def test_both_changes_recompute_full_join(self) -> None:
        plan = build_both_scenario(
            self.tasks,
            revision_bumps={"case-007"},
            size_changes={"case-008": 104},
            v1_config=self.store.v1_resolved,
            current_config_for=self._current_config,
            global_seed=42,
        )
        self.assertTrue(plan["coverage"]["disjoint"])
        self.assertTrue(plan["coverage"]["covers_all"])
        both_records = [
            record for record in plan["tasks"] if record["classification"] == "both"
        ]
        self.assertEqual({record["case_id"] for record in both_records}, {"case-007", "case-008"})
        for record in both_records:
            self.assertEqual(
                set(record["recomputed_stages"]),
                {"model", "sample", "quantize", "forward", "train_backward", "inference"},
            )


if __name__ == "__main__":
    unittest.main()
