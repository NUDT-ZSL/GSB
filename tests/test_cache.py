from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from flash_attn_cache import CacheCorruptError, CacheStore, ResourceLimitError


KEY = "a" * 64
KEY2 = "b" * 64


class CacheTests(unittest.TestCase):
    def test_concurrent_same_key_has_single_effective_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            barrier = threading.Barrier(8)
            results = []
            errors = []

            def writer() -> None:
                try:
                    store = CacheStore(root)
                    barrier.wait()
                    entry = store.put(
                        KEY,
                        {"x": np.arange(8, dtype=np.float64)},
                        {"role": "unit"},
                        source_node_id="node:0",
                    )
                    results.append(entry)
                except Exception as exc:  # pragma: no cover - failure diagnostics
                    errors.append(exc)

            threads = [threading.Thread(target=writer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(errors)
            self.assertEqual(len(results), 8)
            self.assertEqual({entry.key_version for entry in results}, {1})
            self.assertEqual(sum(1 for entry in results if entry.wrote), 1)
            store = CacheStore(root)
            self.assertEqual(store.count_entries(), 1)
            loaded = store.get(KEY)
            self.assertIsNotNone(loaded)
            np.testing.assert_array_equal(loaded.arrays["x"], results[0].arrays["x"])

    def test_corruption_is_detected_then_whole_snapshot_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = CacheStore(Path(td))
            first = store.put(
                KEY,
                {"x": np.arange(8, dtype=np.float64)},
                {"role": "unit"},
            )
            self.assertEqual(first.key_version, 1)
            path = Path(td) / "entries" / f"{KEY}.bin"
            blob = bytearray(path.read_bytes())
            blob[-1] ^= 0xFF
            path.write_bytes(bytes(blob))
            with self.assertRaises(CacheCorruptError) as context:
                store.get(KEY)
            self.assertEqual(context.exception.issue.code, "E_CACHE_CORRUPT")
            replacement = store.put(
                KEY,
                {"x": np.arange(8, dtype=np.float64)},
                {"role": "unit"},
            )
            self.assertTrue(replacement.wrote)
            self.assertEqual(replacement.key_version, 2)
            self.assertIsNotNone(store.get(KEY))

    def test_same_key_new_content_increments_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = CacheStore(Path(td))
            first = store.put(
                KEY,
                {"x": np.array([1.0])},
                {"role": "unit"},
            )
            same = store.put(
                KEY,
                {"x": np.array([1.0])},
                {"role": "unit"},
            )
            changed = store.put(
                KEY,
                {"x": np.array([2.0])},
                {"role": "unit"},
            )
            self.assertEqual(first.key_version, 1)
            self.assertFalse(same.wrote)
            self.assertEqual(same.key_version, 1)
            self.assertTrue(changed.wrote)
            self.assertEqual(changed.key_version, 2)
            self.assertNotEqual(first.content_hash, changed.content_hash)

    def test_entry_limit_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = CacheStore(Path(td), max_entries=1)
            store.put(KEY, {"x": np.array([1.0])}, {"role": "a"})
            with self.assertRaises(ResourceLimitError):
                store.put(KEY2, {"x": np.array([2.0])}, {"role": "b"})
            diagnostics = store.diagnose()
            self.assertTrue(diagnostics.ok)
            self.assertEqual(diagnostics.entries, 1)
            path = Path(td) / "entries" / f"{KEY}.bin"
            blob = bytearray(path.read_bytes())
            blob[0] = 0
            path.write_bytes(bytes(blob))
            diagnostics = store.diagnose()
            self.assertFalse(diagnostics.ok)
            self.assertEqual(diagnostics.corrupt_entries, 1)
            self.assertEqual(diagnostics.issues[0].code, "E_CACHE_CORRUPT")


if __name__ == "__main__":
    unittest.main()
