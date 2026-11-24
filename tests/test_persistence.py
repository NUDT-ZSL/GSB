import tempfile
import unittest
from pathlib import Path

import numpy as np

from dungeon_render import persistence
from dungeon_render.models import TileRect
from tests.test_validation import make_level, make_task
from dungeon_render.validation import parse_level, parse_task


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.level = parse_level(make_level())
        self.task = parse_task(make_task(), self.level)
        self.tile = TileRect(0, 0, 0, 0, 8, 6)
        self.gray = np.arange(48, dtype=np.uint8).reshape(6, 8)
        self.outline = np.zeros((6, 8), dtype=np.uint8)

    def test_roundtrip_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            version = persistence.version_identifier(self.level, self.task)
            persistence.prepare_state_dir(state, version)
            persistence.save_tile(
                state, self.task, self.tile, self.gray, self.outline, version
            )
            loaded_gray, loaded_outline = persistence.load_tile(
                state, self.task, self.tile, version
            )
            np.testing.assert_array_equal(loaded_gray, self.gray)
            np.testing.assert_array_equal(loaded_outline, self.outline)

    def test_corrupt_tile_raises_digest_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            version = persistence.version_identifier(self.level, self.task)
            persistence.prepare_state_dir(state, version)
            persistence.save_tile(
                state, self.task, self.tile, self.gray, self.outline, version
            )
            gray_path, _, _ = persistence.tile_paths(state, 0)
            raw = bytearray(gray_path.read_bytes())
            raw[-1] ^= 0x01
            gray_path.write_bytes(bytes(raw))
            with self.assertRaises(persistence.DungeonError) as caught:
                persistence.load_tile(state, self.task, self.tile, version)
            self.assertEqual(caught.exception.code, "E_DIGEST")

    def test_foreign_version_is_archived_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            old_version = "a" * 64
            new_version = "b" * 64
            persistence.prepare_state_dir(state, old_version)
            persistence.save_tile(
                state, self.task, self.tile, self.gray, self.outline, old_version
            )
            archived = persistence.prepare_state_dir(state, new_version)
            self.assertEqual(archived, [old_version])
            archive_root = state / persistence.ARCHIVE_DIR / old_version
            self.assertTrue(archive_root.exists())
            self.assertTrue(any(archive_root.iterdir()))
            self.assertFalse(
                persistence.tile_paths(state, 0)[0].exists()
            )

    def test_pgm_header_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = np.full((4, 5), 200, dtype=np.uint8)
            target = Path(tmp) / "image.pgm"
            persistence.write_pgm(target, image, "version-xyz")
            header = persistence.read_pgm_header(target)
            self.assertEqual(header["width"], 5)
            self.assertEqual(header["height"], 4)
            self.assertEqual(header["version"], "version-xyz")
            np.testing.assert_array_equal(persistence.read_pgm(target), image)


if __name__ == "__main__":
    unittest.main()
