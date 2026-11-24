from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from audiofp.store import ChunkStore
from audiofp.util import atomic_write_json


class ChunkStoreTests(unittest.TestCase):
    def test_atomic_version_conflict_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = ChunkStore(Path(temp))
            logical = "fp/demo/00000000_00000016"
            missing = store.read_valid(logical, "v1")
            self.assertEqual(missing.status, "missing")
            store.write(logical, "v1", {"items": [1, 2]})
            first = store.read_valid(logical, "v1")
            self.assertEqual(first.status, "valid")
            self.assertEqual(first.payload["items"], [1, 2])
            store.write(logical, "v2", {"items": [3]})
            self.assertEqual(store.read_valid(logical, "v2").status, "valid")
            stale = list((Path(temp) / "stale").rglob("*.json"))
            self.assertTrue(stale)

            target = store.path_for(logical)
            wrapper = json.loads(target.read_text(encoding="utf-8"))
            wrapper["payload"]["items"] = [999]
            atomic_write_json(target, wrapper)
            corrupted = store.read_valid(logical, "v2")
            self.assertEqual(corrupted.status, "invalid")
            self.assertEqual(corrupted.reason, "payload_checksum_mismatch")


if __name__ == "__main__":
    unittest.main()

