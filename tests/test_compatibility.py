from __future__ import annotations

import unittest

from ddp_ckpt_lab.checkpoints import CheckpointStore
from ddp_ckpt_lab.compatibility import assess_compatibility
from ddp_ckpt_lab.errors import BusinessError
from ddp_ckpt_lab.training import Trainer, initialize_training

from tests.helpers import TempWorkspace, make_config


class CompatibilityTests(unittest.TestCase):
    def _checkpoint(self, workspace):
        config = make_config()
        bundle = initialize_training(config)
        trainer = Trainer(config, bundle.dataset, bundle.state)
        trainer.train_until(2)
        store = CheckpointStore(workspace / "checkpoints")
        return config, trainer, store, store.save(trainer.state, config)

    def test_learning_rate_only_is_compatible_migration(self) -> None:
        config = make_config()
        migrated_config = config.with_changes(learning_rate=75)
        result = assess_compatibility(
            config,
            migrated_config,
            source_format_version=2,
            target_format_version=2,
        )
        self.assertTrue(result.compatible)
        self.assertTrue(result.learning_rate_changed)
        self.assertEqual(result.conflicts, ())
        self.assertTrue(any("learning_rate" in step for step in result.migration_steps))

    def test_dimension_or_sharding_conflict_is_e_config(self) -> None:
        config = make_config()
        requested = config.with_changes(hidden_dim=10, sharding="contiguous")
        result = assess_compatibility(
            config,
            requested,
            source_format_version=2,
            target_format_version=2,
        )
        self.assertFalse(result.compatible)
        self.assertEqual(
            {conflict["field"] for conflict in result.conflicts},
            {"hidden_dim", "sharding"},
        )
        with TempWorkspace() as workspace:
            _, _, store, info = self._checkpoint(workspace)
            with self.assertRaises(BusinessError) as load_context:
                store.load(info, requested)
            self.assertEqual(load_context.exception.code, "E_CONFIG")
            fields = {
                conflict["field"]
                for conflict in load_context.exception.details["conflicts"]
            }
            self.assertEqual(fields, {"hidden_dim", "sharding"})

    def test_momentum_change_is_not_migratable(self) -> None:
        config = make_config()
        requested = config.with_changes(momentum=800)
        result = assess_compatibility(
            config,
            requested,
            source_format_version=2,
            target_format_version=2,
        )
        self.assertFalse(result.compatible)
        self.assertEqual(result.conflicts[0]["field"], "momentum")

    def test_runtime_only_fields_do_not_conflict(self) -> None:
        config = make_config()
        requested = config.with_changes(train_steps=20, checkpoint_every=5)
        result = assess_compatibility(
            config,
            requested,
            source_format_version=1,
            target_format_version=2,
        )
        self.assertTrue(result.compatible)
        self.assertFalse(result.learning_rate_changed)
        self.assertTrue(any("v1 -> v2" in step for step in result.migration_steps))

    def test_learning_rate_migration_preserves_payload(self) -> None:
        with TempWorkspace() as workspace:
            config, trainer, store, info = self._checkpoint(workspace)
            migrated_config = config.with_changes(learning_rate=90)
            restored = store.load(info, migrated_config)
            self.assertTrue(restored.migration.learning_rate_changed)
            self.assertEqual(restored.state.payload_digest(), trainer.state.payload_digest())
            self.assertNotEqual(
                restored.state.state_digest(migrated_config),
                trainer.state.state_digest(config),
            )


if __name__ == "__main__":
    unittest.main()
