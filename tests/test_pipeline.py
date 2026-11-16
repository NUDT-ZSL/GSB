from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event

import numpy as np
from PIL import Image

from rsseg.config import load_config
from rsseg.engine import PipelineEngine
from rsseg.errors import E_INDEX_INVALID, E_PATCH_OUT_OF_RANGE, E_RECOMPUTE_BUDGET_PENDING
from rsseg.fusion import RegionPlan, fuse_region, postprocess_classes
from rsseg.index import load_index
from rsseg.model import TileMeta, TileResult
from rsseg.sample import generate_sample
from rsseg.state import StateStore
from rsseg.util import atomic_write_json, read_json


class PipelineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "sample"
        generate_sample(self.workspace, seed=7777)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def set_budget(self, budget: int) -> None:
        path = self.workspace / "config.json"
        config = read_json(path)
        config["recompute_budget"] = budget
        atomic_write_json(path, config)

    def full(self, name: str = "full") -> object:
        state = self.root / name
        shutil.rmtree(state, ignore_errors=True)
        return PipelineEngine(self.workspace, state, mode="full", max_batches=1).run()

    def incremental_until_converged(self, name: str = "inc", limit: int = 50):
        state = self.root / name
        shutil.rmtree(state, ignore_errors=True)
        runs = []
        for _ in range(limit):
            result = PipelineEngine(
                self.workspace,
                state,
                mode="incremental",
                max_batches=1,
            ).run()
            runs.append(result)
            if result.converged:
                return runs
        self.fail("incremental run did not converge")


class DeterminismTests(PipelineTestBase):
    def test_sample_generation_is_byte_deterministic(self) -> None:
        other = self.root / "other"
        generate_sample(other, seed=7777)
        for name in ("tiles.json", "config.json", "overlay.json", "edits.json"):
            self.assertEqual((self.workspace / name).read_bytes(), (other / name).read_bytes())
        for path in sorted((self.workspace / "tiles").glob("*.png")):
            self.assertEqual(path.read_bytes(), (other / "tiles" / path.name).read_bytes())

    def test_concurrency_does_not_change_results(self) -> None:
        one = self.root / "one"
        four = self.root / "four"
        shutil.copytree(self.workspace, one)
        shutil.copytree(self.workspace, four)
        config = read_json(one / "config.json")
        config["concurrency"] = 1
        atomic_write_json(one / "config.json", config)
        config["concurrency"] = 4
        atomic_write_json(four / "config.json", config)
        r1 = PipelineEngine(one, self.root / "state1", mode="full").run()
        r4 = PipelineEngine(four, self.root / "state4", mode="full").run()
        self.assertTrue(r1.converged and r4.converged)
        self.assertTrue(np.array_equal(r1.global_classes, r4.global_classes))
        self.assertEqual(sorted(r1.tile_results), sorted(r4.tile_results))
        for tile_id in r1.tile_results:
            self.assertTrue(np.array_equal(r1.tile_results[tile_id].classes, r4.tile_results[tile_id].classes))
            self.assertTrue(np.array_equal(r1.tile_results[tile_id].scores, r4.tile_results[tile_id].scores))


class BudgetAndSnapshotTests(PipelineTestBase):
    def test_budget_pending_then_pixel_equal_to_full(self) -> None:
        self.set_budget(2)
        full = self.full()
        runs = self.incremental_until_converged()
        self.assertGreater(len(runs), 1)
        self.assertIn(E_RECOMPUTE_BUDGET_PENDING, runs[0].business_codes)
        self.assertFalse(runs[0].converged)
        self.assertTrue(any(batch["reused_tile_count"] for batch in runs[-1].report["batches"]))
        incremental = runs[-1]
        self.assertTrue(incremental.converged)
        self.assertTrue(np.array_equal(incremental.global_classes, full.global_classes))
        for tile_id in full.tile_results:
            self.assertTrue(np.array_equal(incremental.tile_results[tile_id].classes, full.tile_results[tile_id].classes))
            self.assertTrue(np.array_equal(incremental.tile_results[tile_id].scores, full.tile_results[tile_id].scores))

    def test_snapshot_manifest_and_tile_results_are_self_consistent(self) -> None:
        result = self.full()
        store = StateStore(self.root / "full")
        snapshot = store.load_snapshot()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.version, result.snapshot_version)
        self.assertTrue(snapshot.manifest["converged"])
        self.assertTrue(np.array_equal(snapshot.category, result.global_classes))
        self.assertIn("scores_r0_c0", snapshot.tile_results)
        self.assertEqual(len(snapshot.manifest["tiles"]), 9)
        self.assertTrue(all(entry["current"] for entry in snapshot.manifest["tiles"].values()))
        self.assertTrue(all(entry["current"] for entry in snapshot.manifest["fusion"].values()))

    def test_corrupt_snapshot_is_not_reused(self) -> None:
        self.full()
        state = self.root / "full"
        category = state / "snapshot" / "category.npy"
        with open(category, "ab") as fh:
            fh.write(b"corrupt")
        store = StateStore(state)
        self.assertIsNone(store.load_snapshot())
        self.assertTrue(any(f.code == "E_STATE_INVALID" for f in store.recovery_failures))

    def test_backup_directory_is_recovered(self) -> None:
        self.full()
        state = self.root / "full"
        snapshot = state / "snapshot"
        backup = state / ".snapshot-backup"
        os.replace(snapshot, backup)
        recovered = StateStore(state)
        self.assertIsNotNone(recovered.load_snapshot())
        self.assertTrue(snapshot.exists())
        self.assertFalse(backup.exists())

    def test_pre_set_stop_event_writes_interrupted_snapshot_and_exits_zero(self) -> None:
        stop = Event()
        stop.set()
        result = PipelineEngine(
            self.workspace,
            self.root / "interrupted",
            mode="incremental",
            max_batches=1,
            stop_event=stop,
        ).run()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.report["status"], "interrupted")
        snapshot = StateStore(self.root / "interrupted").load_snapshot()
        self.assertIsNotNone(snapshot)


class PatchTests(PipelineTestBase):
    def test_invalid_patch_preserves_old_tile_result_and_fingerprint(self) -> None:
        base = self.full()
        old = base.tile_results["r0_c0"]
        overlay = {
            "schema_version": 1,
            "patches": [
                {
                    "id": "out-of-range",
                    "tile": [0, 0],
                    "bbox": [60, 60, 80, 80],
                    "bands": [[[1, 2]] * 2 for _ in range(4)],
                }
            ],
        }
        atomic_write_json(self.workspace / "overlay.json", overlay)
        result = PipelineEngine(
            self.workspace,
            self.root / "full",
            mode="incremental",
            max_batches=1,
        ).run()
        self.assertIn(E_PATCH_OUT_OF_RANGE, result.business_codes)
        self.assertIn(E_PATCH_OUT_OF_RANGE, [item["code"] for item in result.report["failures"]])
        self.assertTrue(result.converged)
        self.assertEqual(result.report["batches"][0]["recomputed_tile_count"], 0)
        current = result.tile_results["r0_c0"]
        self.assertEqual(current.cache_key, old.cache_key)
        self.assertTrue(np.array_equal(current.classes, old.classes))
        self.assertTrue(np.array_equal(current.scores, old.scores))

    def test_wrong_patch_band_length_is_business_failure(self) -> None:
        self.full()
        overlay = {
            "patches": [
                {
                    "id": "bad-bands",
                    "tile": [0, 1],
                    "bbox": [1, 1, 3, 3],
                    "bands": [[[1, 2], [3, 4]] for _ in range(3)],
                }
            ]
        }
        atomic_write_json(self.workspace / "overlay.json", overlay)
        result = PipelineEngine(
            self.workspace,
            self.root / "full",
            mode="incremental",
            max_batches=1,
        ).run()
        self.assertIn(E_PATCH_OUT_OF_RANGE, result.business_codes)
        self.assertEqual(result.report["batches"][0]["recomputed_tile_count"], 0)

    def test_valid_patch_changes_key_and_is_reproduced_by_full_run(self) -> None:
        base = self.full()
        old_key = base.tile_results["r1_c1"].cache_key
        overlay = {
            "patches": [
                {
                    "id": "valid",
                    "tile": [1, 1],
                    "bbox": [4, 4, 10, 11],
                    "band_deltas": [-0.2, 0.05, -0.3, 0.03],
                }
            ]
        }
        # Create a concrete overlay via the same sequence helper used by the demo.
        from rsseg.sequence import apply_edit

        apply_edit(self.workspace, {"name": "valid-patch", "overlay_ops": overlay["patches"]})
        inc = PipelineEngine(
            self.workspace,
            self.root / "full",
            mode="incremental",
            max_batches=0,
        ).run()
        self.assertTrue(inc.converged)
        self.assertNotEqual(inc.tile_results["r1_c1"].cache_key, old_key)
        fresh = self.full("fresh")
        self.assertTrue(np.array_equal(inc.global_classes, fresh.global_classes))
        self.assertTrue(np.array_equal(inc.tile_results["r1_c1"].scores, fresh.tile_results["r1_c1"].scores))


class ValidationAndFailureTests(PipelineTestBase):
    def test_asymmetric_adjacency_is_index_invalid(self) -> None:
        index_path = self.workspace / "tiles.json"
        index = read_json(index_path)
        index["tiles"][0]["neighbors"].remove([0, 1])
        atomic_write_json(index_path, index)
        parsed = load_index(index_path)
        self.assertTrue(any(f.code == E_INDEX_INVALID for f in parsed.failures))
        self.assertLess(len(parsed.tiles), 9)

    def test_single_bad_tile_is_isolated_and_reported_in_stable_order(self) -> None:
        bad = self.workspace / "tiles" / "r1_c1.png"
        with Image.open(bad) as image:
            image.convert("RGB").save(bad)
        result = PipelineEngine(self.workspace, self.root / "state", mode="full").run()
        failures = result.report["failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["code"], "E_TILE_SHAPE_MISMATCH")
        self.assertEqual((failures[0]["row"], failures[0]["col"]), (1, 1))
        self.assertEqual(len(result.tile_results), 8)
        self.assertFalse(result.converged)

    def test_duplicate_row_col_is_index_invalid(self) -> None:
        index_path = self.workspace / "tiles.json"
        index = read_json(index_path)
        index["tiles"].append(copy.deepcopy(index["tiles"][0]))
        atomic_write_json(index_path, index)
        parsed = load_index(index_path)
        self.assertTrue(any(f.code == E_INDEX_INVALID for f in parsed.failures))


class FusionAndPostprocessTests(PipelineTestBase):
    def _meta(self, row: int, col: int, bounds: tuple[int, int, int, int]) -> TileMeta:
        return TileMeta(
            row=row,
            col=col,
            path=self.workspace / "tiles" / f"r{row}_c{col}.png",
            bounds=bounds,
            neighbors=(),
            band_version="v",
            band_order=("red", "green", "nir", "elevation"),
            expected_shape=(bounds[3] - bounds[1], bounds[2] - bounds[0]),
            adjacency_digest="adj",
        )

    def test_fusion_tie_uses_lexicographic_tile_order(self) -> None:
        m1 = self._meta(0, 0, (0, 0, 2, 2))
        m2 = self._meta(0, 1, (1, 0, 3, 2))
        c1 = np.ones((2, 2), dtype=np.uint8)
        c2 = np.full((2, 2), 2, dtype=np.uint8)
        s = np.zeros((4, 2, 2), dtype=np.float64)
        r1 = TileResult(0, 0, "key1", c1, s, "d1", 1)
        r2 = TileResult(0, 1, "key2", c2, s, "d2", 1)
        config = replace(load_config(self.workspace / "config.json"), fusion_strategy="uniform_vote")
        plan = RegionPlan(0, 0, (0, 0, 2, 2), (m1, m2))
        fused = fuse_region(plan, {"r0_c0": r1, "r0_c1": r2}, config, "dep")
        # At x=1 both votes tie; r0_c0 wins lexicographically.
        self.assertEqual(fused.classes[0, 1], 1)
        self.assertEqual(fused.classes[0, 0], 1)

    def test_small_component_is_filled_from_majority_neighbor(self) -> None:
        classes = np.ones((7, 7), dtype=np.uint8)
        classes[3, 3:5] = 2
        output, replaced = postprocess_classes(classes, minimum_area=3)
        self.assertEqual(replaced, 1)
        self.assertTrue(np.all(output[3, 3:5] == 1))


if __name__ == "__main__":
    unittest.main()
