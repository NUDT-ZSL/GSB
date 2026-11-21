from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dimdiag.engine import DiagnosticEngine
from dimdiag.errors import (
    E_PRECISION_CHANGED,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    BusinessError,
)
from dimdiag.serialization import pretty_json_bytes

from .helpers import base_config, prepare_workspace


class PrecisionAndSnapshotTests(unittest.TestCase):
    def test_precision_switch_invalidates_then_retry_succeeds(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            base = base_config(config_path)
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            pca64 = engine.run(base, base_dir=config_path.parent)
            old_version = pca64.snapshot["snapshot_version"]

            config32 = copy.deepcopy(base)
            config32["precision"]["mode"] = "float32"
            for key in config32["error_tolerance"]:
                config32["error_tolerance"][key] = 1e-2
            with self.assertRaises(BusinessError) as raised:
                engine.run(config32, base_dir=config_path.parent)
            self.assertEqual(raised.exception.code, E_PRECISION_CHANGED)
            self.assertTrue(raised.exception.details["retry_required"])
            self.assertIsNone(engine.store.load_active())
            self.assertEqual(
                engine.store.load_meta()["status"],
                "invalidated",
            )

            retried = engine.run(config32, base_dir=config_path.parent)
            self.assertGreater(retried.snapshot["snapshot_version"], old_version)
            self.assertEqual(retried.snapshot["precision"]["mode"], "float32")

    def test_snapshot_survives_restart(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            first = DiagnosticEngine(root, base_dir=config_path.parent).run(
                config, base_dir=config_path.parent
            )
            second_engine = DiagnosticEngine(root, base_dir=config_path.parent)
            loaded = second_engine.store.load_active()
            self.assertIsNotNone(loaded)
            self.assertEqual(
                loaded["snapshot_version"],
                first.snapshot["snapshot_version"],
            )
            self.assertEqual(
                loaded["overall_checksum"],
                first.snapshot["overall_checksum"],
            )

    def test_snapshot_rollback_is_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            first = engine.run(config, base_dir=config_path.parent)
            old = copy.deepcopy(first.snapshot)
            old["snapshot_version"] -= 1
            with self.assertRaises(BusinessError) as raised:
                engine.store.commit(old)
            self.assertEqual(raised.exception.code, E_SNAPSHOT_ROLLBACK)
            self.assertEqual(
                engine.store.load_active()["snapshot_version"],
                first.snapshot["snapshot_version"],
            )

    def test_snapshot_corruption_is_detected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            engine.run(config, base_dir=config_path.parent)
            active_path = engine.store.active_path
            snapshot = json.loads(active_path.read_text(encoding="utf-8"))
            encoded = snapshot["sections"]["coordinates"]["content"]["values"]
            data = encoded["data_b64"]
            encoded["data_b64"] = ("A" if data[0] != "A" else "B") + data[1:]
            active_path.write_bytes(pretty_json_bytes(snapshot))
            with self.assertRaises(BusinessError) as raised:
                engine.store.load_active()
            self.assertEqual(raised.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

