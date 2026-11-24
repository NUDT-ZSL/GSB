from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from relief_tiles.config import parse_config
from relief_tiles.gray import generate_full_gray
from relief_tiles.renderer import ReliefRenderer
from tests.helpers import config, config_dict


def _block_path(state: Path, version: str, row: int, col: int) -> Path:
    return (
        state
        / "versions"
        / version
        / "blocks"
        / f"r{row:04d}_c{col:04d}.npz"
    )


class PersistenceTests(unittest.TestCase):
    def test_preview_cache_then_resume_reuses_block(self) -> None:
        cfg = config(width=48, height=32, rows=2, cols=3)
        with tempfile.TemporaryDirectory() as temporary:
            renderer = ReliefRenderer(cfg, temporary)
            first_preview = renderer.preview_block(1, 2)
            self.assertFalse(first_preview.cached)
            second_preview = renderer.preview_block(1, 2)
            self.assertTrue(second_preview.cached)
            result = renderer.render_all()
            self.assertEqual(result.report["reused_blocks"], 1)
            self.assertEqual(result.report["recomputed_blocks"], 5)
            self.assertTrue(result.ok)
            self.assertTrue(
                np.array_equal(result.mosaic, generate_full_gray(cfg))
            )

    def test_preview_order_does_not_change_block_content(self) -> None:
        cfg = config(width=48, height=32, rows=2, cols=3)
        blocks = {}
        with tempfile.TemporaryDirectory() as first_state:
            renderer = ReliefRenderer(cfg, first_state)
            for row, col in reversed(list(cfg.iter_block_indices())):
                blocks[(row, col)] = renderer.preview_block(row, col).gray.copy()
        with tempfile.TemporaryDirectory() as second_state:
            renderer = ReliefRenderer(cfg, second_state)
            for row, col in cfg.iter_block_indices():
                preview = renderer.preview_block(row, col)
                self.assertTrue(np.array_equal(preview.gray, blocks[(row, col)]))

    def test_digest_pixel_count_and_missing_file_recovery(self) -> None:
        cfg = config(width=48, height=32, rows=2, cols=3)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            renderer = ReliefRenderer(cfg, str(state))
            renderer.render_all()

            digest_path = _block_path(state, cfg.version, 0, 0)
            with np.load(digest_path, allow_pickle=False) as archive:
                gray = np.asarray(archive["gray"]).copy()
                metadata = np.asarray(archive["metadata"], dtype=np.uint8).tobytes()
            gray[0, 0] ^= 1
            np.savez(
                digest_path,
                gray=gray,
                metadata=np.frombuffer(metadata, dtype=np.uint8),
            )

            pixel_path = _block_path(state, cfg.version, 0, 1)
            with np.load(pixel_path, allow_pickle=False) as archive:
                pixel_gray = np.asarray(archive["gray"]).copy()
                pixel_meta = json.loads(
                    np.asarray(archive["metadata"], dtype=np.uint8).tobytes()
                )
            pixel_meta["pixel_count"] += 1
            np.savez(
                pixel_path,
                gray=pixel_gray,
                metadata=np.frombuffer(
                    json.dumps(
                        pixel_meta,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                    dtype=np.uint8,
                ),
            )

            _block_path(state, cfg.version, 0, 2).unlink()
            resumed = ReliefRenderer(cfg, str(state)).render_all()
            reasons = {(item["row"], item["col"]): item["reason"] for item in resumed.report["invalid_blocks"]}
            self.assertEqual(reasons[(0, 0)], "digest_mismatch")
            self.assertEqual(reasons[(0, 1)], "pixel_count_mismatch")
            self.assertEqual(reasons[(0, 2)], "missing")
            self.assertTrue(all(item["code"] == "E_DIGEST" for item in resumed.report["invalid_blocks"]))
            self.assertEqual(resumed.report["recomputed_blocks"], 3)
            self.assertTrue(np.array_equal(resumed.mosaic, generate_full_gray(cfg)))

    def test_version_change_archives_old_blocks(self) -> None:
        first_value = config_dict(seed=100, width=48, height=32, rows=2, cols=3)
        first = parse_config(first_value)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            ReliefRenderer(first, str(state)).render_all()
            old_dir = state / "versions" / first.version
            self.assertTrue(old_dir.exists())

            second_value = json.loads(json.dumps(first_value))
            second_value["seed"] = 101
            second = parse_config(second_value)
            ReliefRenderer(second, str(state))
            self.assertNotEqual(first.version, second.version)
            self.assertFalse(old_dir.exists())
            archived = list((state / "archive").iterdir())
            self.assertEqual(len(archived), 1)
            self.assertTrue(archived[0].name.startswith(first.version))
            self.assertTrue((state / "versions" / second.version).exists())

    def test_reference_generation_is_deterministic(self) -> None:
        from relief_tiles.samples import generate_samples

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_result = generate_samples(first)
            second_result = generate_samples(second)
            self.assertEqual(
                first_result["config_version"], second_result["config_version"]
            )
            for filename in (
                "config.json",
                "pattern.json",
                "reference_height.npy",
                "reference_gray.npy",
                "reference_full.png",
                "sample_manifest.json",
            ):
                self.assertEqual(
                    (Path(first) / filename).read_bytes(),
                    (Path(second) / filename).read_bytes(),
                    filename,
                )


if __name__ == "__main__":
    unittest.main()
