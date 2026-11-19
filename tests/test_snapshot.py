from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from dialogue_risk.errors import AppError
from dialogue_risk.service import DomainService
from dialogue_risk.snapshot import SnapshotStore
from tests.helpers import DATA, write_json


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runtime = self.root / "runtime"
        self.service = DomainService(DATA, self.runtime)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_old_snapshot_cannot_overwrite_new(self) -> None:
        old_path = self.root / "old.json"
        shutil.copy2(self.service.store.path, old_path)
        self.service.edit_text("conv-0001", 1, "new version")
        with self.assertRaises(AppError) as context:
            self.service.verify_snapshot_file(old_path)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(context.exception.details["category"], "results")

    def test_corrupt_snapshot_names_category_and_version(self) -> None:
        corrupt_path = self.root / "corrupt.json"
        payload = json.loads(self.service.store.path.read_text(encoding="utf-8"))
        payload["risk_words"]["terms"]["fraud"][0]["severity"] = 99.0
        corrupt_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(AppError) as context:
            SnapshotStore(corrupt_path).load()
        self.assertEqual(context.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(context.exception.details["category"], "risk")
        self.assertEqual(context.exception.details["version"], 1)

    def test_restart_replaces_when_sample_version_increases(self) -> None:
        temp_data = self.root / "data"
        shutil.copytree(DATA, temp_data)
        config = json.loads((temp_data / "config.json").read_text(encoding="utf-8"))
        config["config_version"] = 2
        write_json(temp_data / "config.json", config)
        old_version = self.service._snapshot.snapshot_version
        restarted = DomainService(temp_data, self.runtime)
        self.assertGreater(restarted._snapshot.snapshot_version, old_version)
        self.assertEqual(restarted._snapshot.config_version, 2)
        restarted.self_check()

    def test_snapshot_restore_rejects_rollback(self) -> None:
        old_path = self.root / "old.json"
        shutil.copy2(self.service.store.path, old_path)
        self.service.edit_weight("conv-0001", 1, 1.2)
        with self.assertRaises(AppError) as context:
            self.service.restore_snapshot(old_path)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")


if __name__ == "__main__":
    unittest.main()

