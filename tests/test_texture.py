from __future__ import annotations

import unittest

import numpy as np

from sceneopt.errors import E_TEXTURE_BLOCK_MISMATCH, TextureBlockMismatch
from sceneopt.models import CameraPose
from sceneopt.texture import bake_texture_paths, compare_texture_paths


class TextureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vertices = np.asarray(
            (
                (-1.0, -1.0, 0.0),
                (1.0, -1.0, 0.0),
                (1.0, 1.0, 0.0),
                (-1.0, 1.0, 0.0),
            ),
            dtype=np.float64,
        )
        self.faces = np.asarray(((0, 1, 2), (0, 2, 3)), dtype=np.int64)
        self.uv = np.asarray(
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            dtype=np.float64,
        )
        rotation = np.asarray(
            (
                (1.0, 0.0, 0.0),
                (0.0, -1.0, 0.0),
                (0.0, 0.0, -1.0),
            ),
            dtype=np.float64,
        )
        intrinsics = np.asarray(
            (
                (30.0, 0.0, 3.5),
                (0.0, 30.0, 3.5),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        self.camera = CameraPose(
            camera_id="texture-cam",
            rotation=rotation,
            translation=np.asarray((0.0, 0.0, 3.0)),
            intrinsics=intrinsics,
            observation_seed=7,
            image_height=8,
            image_width=8,
        )
        yy, xx = np.meshgrid(
            np.linspace(0.0, 1.0, 8),
            np.linspace(0.0, 1.0, 8),
            indexing="ij",
        )
        self.image = np.stack((xx, yy, 0.5 * (xx + yy)), axis=2)

    def _bake(self) -> object:
        return bake_texture_paths(
            self.vertices,
            self.faces,
            self.uv,
            (self.camera,),
            {self.camera.camera_id: self.image},
            texture_seed=33,
            height=8,
            width=8,
            block_height=3,
            block_width=3,
            multi_view_tolerance=0.75,
            scene_id="texture-scene",
        )

    def test_block_and_full_paths_are_byte_exact(self) -> None:
        result = self._bake()
        comparison = compare_texture_paths(
            result.full_texture, result.block_texture, scene_id="texture-scene"
        )
        self.assertTrue(comparison["exact_equal"])
        self.assertEqual(
            result.full_texture.tobytes(), result.block_texture.tobytes()
        )

    def test_mismatch_reports_first_pixel_and_spectrum(self) -> None:
        result = self._bake()
        damaged = result.block_texture.copy()
        damaged[2, 3, 1] += 1e-9
        with self.assertRaises(TextureBlockMismatch) as context:
            compare_texture_paths(
                result.full_texture, damaged, scene_id="texture-scene"
            )
        error = context.exception
        self.assertEqual(error.code, E_TEXTURE_BLOCK_MISMATCH)
        self.assertEqual(error.details["first_pixel"], [2, 3])
        self.assertGreater(
            error.details["deviation_spectrum"]["max"], 0.0
        )


if __name__ == "__main__":
    unittest.main()

