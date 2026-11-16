from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

import numpy as np

from ddp_ckpt_lab.checkpoints import CheckpointStore, probe_atomic_storage
from ddp_ckpt_lab.errors import BusinessError
from ddp_ckpt_lab.training import Trainer, initialize_training

from tests.helpers import TempWorkspace, make_config


class CheckpointTests(unittest.TestCase):
    def _trained(self, step: int = 2):
        config = make_config()
        bundle = initialize_training(config)
        trainer = Trainer(config, bundle.dataset, bundle.state)
        trainer.train_until(step)
        return config, bundle, trainer

    def test_atomic_v2_checkpoint_and_restore(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            storage = store.ensure_ready()
            self.assertTrue(storage["atomic_directory_rename"])
            info = store.save(trainer.state, config)
            self.assertEqual(info.format_version, 2)
            self.assertEqual(info.global_step, 2)
            self.assertEqual(len(info.shard_digests), 4)
            path = Path(info.path)
            self.assertTrue((path / "COMPLETE").is_file())
            self.assertEqual(
                [entry.name for entry in store.root.iterdir() if entry.name.startswith(".tmp-")],
                [],
            )
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["format_version"], 2)
            self.assertEqual(manifest["state_digest"], trainer.state.state_digest(config))
            self.assertEqual(manifest["workers"], 4)
            self.assertEqual(store.scan().residues, ())
            restored = store.load(info, config)
            self.assertEqual(
                restored.state.state_digest(config),
                trainer.state.state_digest(config),
            )
            self.assertEqual(
                restored.state.payload_digest(), trainer.state.payload_digest()
            )
            self.assertEqual(restored.migration.strategy, "replace_rebuild")

    def test_duplicate_checkpoint_is_e_checkpoint(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            store.save(trainer.state, config)
            with self.assertRaises(BusinessError) as context:
                store.save(trainer.state, config)
            self.assertEqual(context.exception.code, "E_CHECKPOINT")

    def test_incomplete_residue_is_e_checkpoint(self) -> None:
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            store.ensure_ready()
            residue = store.root / ".tmp-checkpoint_00000009-crash"
            residue.mkdir()
            (residue / "manifest.json").write_text("{", encoding="utf-8")
            scan = store.scan()
            self.assertEqual(len(scan.residues), 1)
            self.assertIn("incomplete_atomic_write_residue", scan.residues[0]["reason"])
            with self.assertRaises(BusinessError) as context:
                store.list_checkpoints(strict=True)
            self.assertEqual(context.exception.code, "E_CHECKPOINT")

    def test_failed_atomic_writer_leaves_no_final_checkpoint(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            store.ensure_ready()

            def broken_writer(temp_path: Path) -> None:
                (temp_path / "manifest.json").write_text("{", encoding="utf-8")
                raise RuntimeError("simulated interruption")

            with self.assertRaises(BusinessError) as context:
                store._commit_directory("checkpoint_00000002", broken_writer)
            self.assertEqual(context.exception.code, "E_CHECKPOINT")
            self.assertFalse((store.root / "checkpoint_00000002").exists())
            self.assertEqual(
                [entry for entry in store.root.iterdir() if entry.name.startswith(".tmp-")],
                [],
            )

    def test_corrupt_shard_is_e_restore(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            info = store.save(trainer.state, config)
            shard_path = Path(info.path) / info.shard_digests[0]["file"]
            with np.load(shard_path, allow_pickle=False) as archive:
                arrays = {name: np.asarray(archive[name]).copy() for name in archive.files}
            value = arrays["param__W1"].reshape(-1)[0]
            arrays["param__W1"].reshape(-1)[0] = np.nextafter(value, np.inf)
            with shard_path.open("wb") as stream:
                np.savez(stream, **arrays)
            with self.assertRaises(BusinessError) as context:
                store.load(info, config)
            self.assertEqual(context.exception.code, "E_RESTORE")
            self.assertIn("worker_id", context.exception.details)

    def test_manifest_tamper_is_e_checkpoint_scan(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            info = store.save(trainer.state, config)
            manifest_path = Path(info.path) / "manifest.json"
            manifest_path.write_text(
                manifest_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(BusinessError) as context:
                store.list_checkpoints(strict=True)
            self.assertEqual(context.exception.code, "E_CHECKPOINT")

    def test_v1_migration_rebuilds_fresh_state(self) -> None:
        config, _, trainer = self._trained(2)
        with TempWorkspace() as workspace:
            store = CheckpointStore(workspace / "checkpoints")
            info = store.save_legacy_v1(trainer.state, config)
            self.assertEqual(info.format_version, 1)
            restored = store.load(info, config)
            self.assertEqual(restored.migration.source_format_version, 1)
            self.assertEqual(restored.migration.target_format_version, 2)
            self.assertEqual(restored.migration.strategy, "replace_rebuild")
            self.assertEqual(
                restored.state.payload_digest(), trainer.state.payload_digest()
            )
            self.assertIsNot(restored.state, trainer.state)
            for left, right in zip(restored.state.workers, trainer.state.workers):
                for name in left.parameters:
                    self.assertFalse(left.parameters[name] is right.parameters[name])
                    self.assertTrue(
                        np.array_equal(left.parameters[name], right.parameters[name])
                    )
                    self.assertTrue(
                        np.array_equal(left.momentum[name], right.momentum[name])
                    )

    def test_probe_atomic_storage_cleans_up(self) -> None:
        with TempWorkspace() as workspace:
            result = probe_atomic_storage(workspace / "probe")
            self.assertTrue(result["writable"])
            leftovers = [
                entry
                for entry in (workspace / "probe").iterdir()
                if entry.name.startswith(".tmp-")
            ]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
