from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from repairflow.config import normalize_config
from repairflow.demo import run_demo
from repairflow.engine import RepairEngine
from repairflow.errors import BusinessError, ErrorCode
from repairflow.graph import (
    NODE_BLEND_OUTPUT,
    NODE_CONTENT_COMPLETION,
    NODE_CONTEXT_SAMPLE,
    NODE_MASK_PARSE,
    NODE_ORDER,
    NODE_STYLE_TRANSFER,
)
from repairflow.mask import MaskEditor, MaskState, Region
from repairflow.nodes import assert_blend_consistency
from repairflow.samples import build_sample_payload
from repairflow.snapshots import SnapshotStore


def make_base() -> np.ndarray:
    height, width = 36, 48
    yy, xx = np.mgrid[0:height, 0:width]
    base = np.stack(
        (
            48 + xx * 3,
            32 + yy * 4,
            90 + ((xx + yy) % 17) * 5,
        ),
        axis=-1,
    )
    return np.clip(base, 0, 255).astype(np.uint8)


def rectangle(shape: tuple[int, int], y0: int, x0: int, y1: int, x1: int) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.bool_)
    mask[y0:y1, x0:x1] = True
    return mask


def make_masks() -> MaskState:
    shape = (36, 48)
    return MaskState(
        (
            Region(1, rectangle(shape, 5, 5, 12, 15)),
            Region(2, rectangle(shape, 7, 28, 16, 39)),
            Region(3, rectangle(shape, 22, 14, 30, 27)),
        )
    )


def test_config(**overrides: object) -> dict:
    config = {
        "context_radius": 2,
        "style": {
            "style_strength": 0.35,
            "color_matrix": [
                [1.04, 0.01, -0.01],
                [0.01, 0.98, 0.02],
                [-0.01, 0.02, 1.03],
            ],
            "edge_preserve": 0.6,
            "iterations": 2,
            "precision": "high",
            "blend": {"feather_px": 2},
        },
        "limits": {
            "max_regions": 10,
            "max_resident_pixels": 10_000_000,
            "max_parallel_regions": 4,
            "resident_node_copies": 6,
        },
    }
    config.update(overrides)
    return normalize_config(config)


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base = make_base()
        self.masks = make_masks()
        self.config = test_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def engine(self, name: str = "cache", **config_overrides: object) -> RepairEngine:
        return RepairEngine(
            self.base,
            cache_dir=self.root / name,
            config=test_config(**config_overrides) if config_overrides else self.config,
        )

    def test_initial_full_recompute_byte_and_artifact_identical(self) -> None:
        engine = self.engine()
        result = engine.submit(self.masks)
        comparison = engine.compare_with_full(raise_on_mismatch=True)
        self.assertTrue(comparison["byte_identical"])
        self.assertTrue(comparison["artifact_hashes_equal"])
        self.assertEqual(result.report["recomputed_regions"], [1, 2, 3])
        self.assertEqual(result.report["reused_regions"], [])
        self.assertTrue(result.report["self_check"]["ok"])

        second = engine.submit(self.masks)
        self.assertEqual(second.report["recomputed_regions"], [])
        self.assertEqual(second.report["reused_regions"], [1, 2, 3])
        np.testing.assert_array_equal(result.image, second.image)

    def test_grow_only_recomputes_target_and_unrelated_reuses(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        grown = MaskEditor.grow(self.masks, 1, 2)
        result = engine.submit(grown)
        self.assertEqual(result.report["recomputed_regions"], [1])
        self.assertEqual(result.report["reused_regions"], [2, 3])
        self.assertEqual(
            result.report["node_status"]["1"][NODE_MASK_PARSE], "recomputed"
        )
        self.assertEqual(result.report["node_status"]["2"][NODE_MASK_PARSE], "cache_hit")
        self.assertEqual(result.report["node_status"]["3"][NODE_BLEND_OUTPUT], "cache_hit")
        comparison = engine.compare_with_full(raise_on_mismatch=True)
        self.assertTrue(comparison["byte_identical"])
        self.assertTrue(comparison["artifact_hashes_equal"])

    def test_shrink_does_not_invalidate_unrelated_regions(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        shrunk = MaskEditor.shrink(self.masks, 2, 2)
        result = engine.submit(shrunk)
        self.assertEqual(result.report["recomputed_regions"], [2])
        self.assertEqual(result.report["reused_regions"], [1, 3])
        self.assertEqual(
            result.report["node_status"]["1"][NODE_BLEND_OUTPUT], "cache_hit"
        )
        self.assertEqual(
            result.report["node_status"]["3"][NODE_CONTENT_COMPLETION], "cache_hit"
        )
        self.assertTrue(engine.compare_with_full(raise_on_mismatch=True)["byte_identical"])

    def test_style_change_only_recomputes_style_and_blend(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        changed = copy.deepcopy(self.config)
        changed["style"]["style_strength"] = 0.61
        result = engine.submit(self.masks, style=changed)
        for region_id in ("1", "2", "3"):
            self.assertEqual(
                result.report["recomputed_nodes"][region_id],
                [NODE_STYLE_TRANSFER, NODE_BLEND_OUTPUT],
            )
            self.assertEqual(
                result.report["reused_nodes"][region_id],
                [NODE_MASK_PARSE, NODE_CONTEXT_SAMPLE, NODE_CONTENT_COMPLETION],
            )
        self.assertEqual(result.report["invalidated_regions"], [1, 2, 3])
        self.assertTrue(engine.compare_with_full(raise_on_mismatch=True)["byte_identical"])

    def test_merge_target_recomputed_and_source_removed(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        merged = MaskEditor.merge(self.masks, (2, 3), target_id=2)
        result = engine.submit(merged)
        self.assertEqual(result.report["recomputed_regions"], [2])
        self.assertEqual(result.report["reused_regions"], [1])
        self.assertEqual(result.report["removed_regions"], [3])
        self.assertNotIn(3, result.report["affected_regions"])
        comparison = engine.compare_with_full(raise_on_mismatch=True)
        self.assertTrue(comparison["byte_identical"])
        self.assertTrue(comparison["artifact_hashes_equal"])

    def test_cache_key_composition_and_cache_hit_roundtrip(self) -> None:
        engine = self.engine()
        first = engine.submit(self.masks)
        inspect = engine.inspect_region(1)
        for node in NODE_ORDER:
            components = inspect["nodes"][node]["key_components"]
            self.assertIn("base_hash", components)
            self.assertIn("region_mask_digest", components)
            self.assertIn("context_radius", components)
            self.assertIn("style_param_version", components)
            self.assertIn("upstream_artifact_hashes", components)
            self.assertEqual(components["region_mask_digest"], inspect["region"]["digest"])
            self.assertRegex(inspect["nodes"][node]["artifact_hash"], r"^[0-9a-f]{64}$")

        mask_key_before = inspect["nodes"][NODE_MASK_PARSE]["key"]
        style_key_before = inspect["nodes"][NODE_STYLE_TRANSFER]["key"]
        strength_update = copy.deepcopy(self.config)
        strength_update["style"]["style_strength"] = 0.75
        engine.submit(self.masks, style=strength_update)
        inspect_changed = engine.inspect_region(1)
        self.assertEqual(
            inspect_changed["nodes"][NODE_MASK_PARSE]["key"], mask_key_before
        )
        self.assertNotEqual(
            inspect_changed["nodes"][NODE_STYLE_TRANSFER]["key"], style_key_before
        )

        roundtrip = engine.submit(self.masks, style=self.config)
        self.assertEqual(roundtrip.report["recomputed_regions"], [])
        self.assertEqual(roundtrip.report["reused_regions"], [1, 2, 3])
        self.assertEqual(
            roundtrip.report["node_status"]["1"][NODE_STYLE_TRANSFER], "cache_hit"
        )
        np.testing.assert_array_equal(roundtrip.image, first.image)

    def test_blend_weights_are_inside_mask_and_sum_to_one(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        inspection = engine.inspect_region(1)
        record = inspection["nodes"][NODE_BLEND_OUTPUT]
        artifact, _ = engine.cache.load(record["key"])
        y0, x0, y1, x1 = inspection["region"]["bbox"]
        scene_mask = self.masks.by_id()[1].mask
        outside = ~scene_mask[y0:y1, x0:x1]
        self.assertTrue(np.all(artifact[..., 3][outside] == 0.0))
        self.assertGreaterEqual(float(np.min(artifact[..., 3])), 0.0)
        self.assertLessEqual(float(np.max(artifact[..., 3])), 1.0)
        np.testing.assert_array_equal(artifact[..., 3] + (1.0 - artifact[..., 3]), 1.0)

        regionwise = engine.image
        tampered = regionwise.copy()
        row = int(np.argwhere(scene_mask)[0][0])
        col = int(np.argwhere(scene_mask)[0][1])
        tampered[row, col, 0] ^= 1
        with self.assertRaises(BusinessError) as context:
            assert_blend_consistency(
                regionwise,
                tampered,
                {region.region_id: region.mask for region in self.masks.regions},
            )
        self.assertEqual(context.exception.code, ErrorCode.BLEND_MISMATCH)
        self.assertEqual(context.exception.details["coordinate"], [row, col])
        self.assertEqual(context.exception.details["region_id"], 1)

    def test_stage_busy_is_immediate(self) -> None:
        engine = self.engine()
        engine.stage_lock.acquire()
        try:
            with self.assertRaises(BusinessError) as context:
                engine.submit(self.masks)
        finally:
            engine.stage_lock.release()
        self.assertEqual(context.exception.code, ErrorCode.STAGE_BUSY)

    def test_resource_limits_and_serial_degradation(self) -> None:
        with self.assertRaises(BusinessError) as count_error:
            self.engine("limit-regions", limits={
                "max_regions": 2,
                "max_resident_pixels": 10_000_000,
                "max_parallel_regions": 4,
                "resident_node_copies": 6,
            }).submit(self.masks)
        self.assertEqual(count_error.exception.code, ErrorCode.RESOURCE_LIMIT)

        with self.assertRaises(BusinessError) as memory_error:
            self.engine("limit-memory", limits={
                "max_regions": 10,
                "max_resident_pixels": 10,
                "max_parallel_regions": 4,
                "resident_node_copies": 6,
            }).submit(self.masks)
        self.assertEqual(memory_error.exception.code, ErrorCode.RESOURCE_LIMIT)

        degraded_engine = self.engine("degraded", limits={
            "max_regions": 10,
            "max_resident_pixels": 10_000_000,
            "max_parallel_regions": 1,
            "resident_node_copies": 6,
        })
        result = degraded_engine.submit(self.masks)
        self.assertTrue(result.report["degraded"])
        self.assertIn(
            "parallel_region_limit_serial",
            result.report["degradation"]["categories"],
        )
        self.assertEqual(result.report["recomputed_regions"], [1, 2, 3])
        self.assertEqual(result.report["degradation"]["omitted_regions"], [])

    def test_validation_codes_and_fields(self) -> None:
        engine = self.engine()
        with self.assertRaises(BusinessError) as duplicate:
            MaskState(
                (
                    Region(1, rectangle((36, 48), 1, 1, 3, 3)),
                    Region(1, rectangle((36, 48), 5, 5, 7, 7)),
                )
            )
        self.assertEqual(duplicate.exception.code, ErrorCode.DUPLICATE_REGION)
        self.assertEqual(duplicate.exception.details["region_id"], 1)

        with self.assertRaises(BusinessError) as empty:
            engine.submit(MaskState(()))
        self.assertEqual(empty.exception.code, ErrorCode.EMPTY_MASK)
        self.assertIn("field", empty.exception.details)

        with self.assertRaises(BusinessError) as bounds:
            engine.submit(MaskState.from_mapping({7: rectangle((4, 4), 1, 1, 3, 3)}))
        self.assertEqual(bounds.exception.code, ErrorCode.REGION_OUT_OF_BOUNDS)
        self.assertEqual(bounds.exception.details["region_id"], 7)
        self.assertEqual(bounds.exception.details["field"], "mask.shape")

        cases = [
            (
                ErrorCode.STYLE_STRENGTH_RANGE,
                {"style": {"style_strength": -0.1}},
                "style.style_strength",
            ),
            (
                ErrorCode.UNKNOWN_PRECISION,
                {"style": {"precision": "quantum"}},
                "style.precision",
            ),
            (
                ErrorCode.INVALID_ITERATIONS,
                {"style": {"iterations": 0}},
                "style.iterations",
            ),
            (
                ErrorCode.INVALID_CONTEXT_RADIUS,
                {"context_radius": 0},
                "context_radius",
            ),
        ]
        for code, payload, field in cases:
            with self.subTest(code=code):
                with self.assertRaises(BusinessError) as context:
                    normalize_config(payload)
                self.assertEqual(context.exception.code, code)
                self.assertEqual(context.exception.details["field"], field)
                self.assertIn("region_id", context.exception.details)

    def test_region_node_failure_is_attributed(self) -> None:
        engine = self.engine("failure")
        with mock.patch(
            "repairflow.engine.content_completion",
            side_effect=RuntimeError("synthetic node failure"),
        ):
            with self.assertRaises(BusinessError) as context:
                engine.submit(self.masks)
        self.assertEqual(context.exception.code, ErrorCode.REGION_NODE_FAILURE)
        self.assertIn(context.exception.details["region_id"], [1, 2, 3])
        self.assertEqual(
            context.exception.details["node"], NODE_CONTENT_COMPLETION
        )

    def test_snapshot_versions_are_monotonic(self) -> None:
        engine = self.engine()
        first = engine.submit(self.masks)
        self.assertEqual(first.report["snapshots"]["mask"]["version"], 1)
        self.assertEqual(first.report["snapshots"]["style"]["version"], 1)
        changed = copy.deepcopy(self.config)
        changed["style"]["style_strength"] = 0.7
        second = engine.submit(self.masks, style=changed)
        self.assertEqual(second.report["snapshots"]["mask"]["version"], 1)
        self.assertEqual(second.report["snapshots"]["style"]["version"], 2)
        self.assertEqual(second.report["snapshots"]["image"]["version"], 2)
        grown = MaskEditor.grow(self.masks, 1, 1)
        third = engine.submit(grown, style=changed)
        self.assertEqual(third.report["snapshots"]["mask"]["version"], 2)
        self.assertEqual(third.report["snapshots"]["style"]["version"], 2)
        self.assertGreaterEqual(third.report["snapshots"]["image"]["version"], 3)
        self.assertTrue(engine.verify_snapshots()["ok"])

    def test_self_check_detects_overlap(self) -> None:
        engine = self.engine()
        engine.submit(self.masks)
        damaged = copy.deepcopy(engine.last_report)
        damaged["reused_regions"] = [damaged["recomputed_regions"][0]]
        with self.assertRaises(BusinessError) as context:
            engine.self_check(damaged)
        self.assertEqual(context.exception.code, ErrorCode.INCONSISTENT)
        self.assertTrue(
            any(
                failure["code"] == "RECOMPUTED_REUSED_OVERLAP"
                for failure in context.exception.details["failures"]
            )
        )

    def test_standard_precision_is_deterministic(self) -> None:
        config = copy.deepcopy(self.config)
        config["style"]["precision"] = "standard"
        engine1 = RepairEngine(self.base, self.root / "p1", config)
        engine2 = RepairEngine(self.base, self.root / "p2", config)
        one = engine1.submit(self.masks)
        two = engine2.submit(self.masks)
        np.testing.assert_array_equal(one.image, two.image)
        self.assertTrue(engine1.compare_with_full(raise_on_mismatch=True)["byte_identical"])


class SnapshotStoreTestCase(unittest.TestCase):
    def test_rollback_corruption_and_whole_replacement(self) -> None:
        store = SnapshotStore()
        first = make_masks()
        grown = MaskEditor.grow(first, 1, 1)
        store.commit("mask", 1, first)
        store.commit("mask", 2, grown)

        with self.assertRaises(BusinessError) as rollback:
            store.commit("mask", 1, first)
        self.assertEqual(rollback.exception.code, ErrorCode.SNAPSHOT_ROLLBACK)
        self.assertEqual(store.current_version("mask"), 2)
        self.assertEqual(store.get("mask").checksum, grown.checksum)

        with self.assertRaises(BusinessError) as corrupt:
            store.commit("mask", 3, grown, expected_checksum="0" * 64)
        self.assertEqual(corrupt.exception.code, ErrorCode.SNAPSHOT_CORRUPT)
        self.assertEqual(store.current_version("mask"), 2)

        with self.assertRaises(BusinessError) as conflicting:
            store.commit("mask", 2, first)
        self.assertEqual(conflicting.exception.code, ErrorCode.SNAPSHOT_CORRUPT)

        store.commit("style", 1, {"style_strength": 0.4})
        style_snapshot = store.current("style")
        style_snapshot.payload["style_strength"] = 0.9
        with self.assertRaises(BusinessError) as damaged:
            store.verify("style")
        self.assertEqual(damaged.exception.code, ErrorCode.SNAPSHOT_CORRUPT)

    def test_snapshot_is_whole_object_clone(self) -> None:
        store = SnapshotStore()
        masks = make_masks()
        store.commit("mask", 1, masks)
        retrieved = store.get("mask")
        self.assertEqual(retrieved.checksum, masks.checksum)
        self.assertIsNot(retrieved, masks)


class SampleBundleTestCase(unittest.TestCase):
    def test_fixed_seed_sample_is_repeatable(self) -> None:
        base1, masks1, config1 = build_sample_payload(
            seed=123, width=64, height=48
        )
        base2, masks2, config2 = build_sample_payload(
            seed=123, width=64, height=48
        )
        np.testing.assert_array_equal(base1, base2)
        self.assertEqual(masks1.checksum, masks2.checksum)
        self.assertEqual(config1, config2)
        self.assertEqual(base1.shape, (48, 64, 3))

    def test_end_to_end_demo_acceptance(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as workdir:
            report = run_demo(
                workdir,
                project_root / "samples" / "data",
                fresh_cache=True,
            )
        self.assertTrue(report["all_passed"])
        self.assertTrue(report["acceptance"]["incremental_equals_full_for_all_steps"])
        self.assertTrue(report["acceptance"]["snapshot_rollback_detected"])
        self.assertTrue(report["acceptance"]["stage_busy_detected"])


if __name__ == "__main__":
    unittest.main()
