from __future__ import annotations

import unittest

import numpy as np

from diagnostic_app.pipeline import (
    build_context,
    generate_block_pixels,
    run_global,
)
from diagnostic_app.planner import build_block_descriptors
from diagnostic_app.samples import sample_config, sample_tasks


class PipelineDeterminismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = sample_tasks()
        self.config = sample_config()

    def test_global_generation_is_bytewise_deterministic(self) -> None:
        for task in self.tasks:
            with self.subTest(task_id=task["task_id"]):
                first = run_global(task, self.config, history=True)
                second = run_global(task, self.config, history=True)
                self.assertEqual(first["image"].tobytes(), second["image"].tobytes())
                self.assertEqual(first["final_field"].tobytes(), second["final_field"].tobytes())
                self.assertEqual(
                    [item.tobytes() for item in first["history"]],
                    [item.tobytes() for item in second["history"]],
                )

    def test_block_generation_matches_global_for_multiple_grids(self) -> None:
        for task in self.tasks:
            context = build_context(task, self.config)
            global_result = run_global(task, self.config, context=context)
            height, width, channels = task["image_shape"]
            for rows, cols in ((1, 1), (2, 2), (3, 3)):
                with self.subTest(task_id=task["task_id"], grid=f"{rows}x{cols}"):
                    descriptors = build_block_descriptors(height, width, rows, cols)
                    stitched = np.zeros((height, width, channels), dtype=context.dtype)
                    for descriptor in descriptors:
                        product = generate_block_pixels(
                            context,
                            descriptor["row_start"], descriptor["row_end"],
                            descriptor["col_start"], descriptor["col_end"],
                        )
                        stitched[
                            descriptor["row_start"]:descriptor["row_end"],
                            descriptor["col_start"]:descriptor["col_end"],
                        ] = product["image"]
                    self.assertEqual(stitched.tobytes(), global_result["image"].tobytes())

    def test_anchor_path_also_matches_blocks(self) -> None:
        task = self.tasks[1]
        reference = np.linspace(0.0, 1.0, int(np.prod(task["image_shape"])), dtype=np.float32)
        reference = reference.reshape(task["image_shape"])
        context = build_context(task, self.config, reference=reference, anchor_strength=0.2)
        global_result = run_global(task, self.config, context=context)
        descriptors = build_block_descriptors(*task["image_shape"][:2], 3, 3)
        for descriptor in descriptors:
            product = generate_block_pixels(
                context,
                descriptor["row_start"], descriptor["row_end"],
                descriptor["col_start"], descriptor["col_end"],
            )
            expected = global_result["image"][
                descriptor["row_start"]:descriptor["row_end"],
                descriptor["col_start"]:descriptor["col_end"],
            ]
            self.assertEqual(product["image"].tobytes(), expected.tobytes())


if __name__ == "__main__":
    unittest.main()
