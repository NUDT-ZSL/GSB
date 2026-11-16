import tempfile
import unittest
from pathlib import Path

import numpy as np

from panseg.converters import downgrade_v2_to_v1, upgrade_v1_to_v2
from panseg.fusion import fuse_scene
from panseg.models import V1, V2, InstanceRecord, PipelineConfig
from panseg.rle import bitmap_to_runs
from panseg.v1_package import parse_v1_package
from panseg.v2_package import parse_v2_package


def _record(instance_id, source, bitmap, semantic="car", confidence=0.9, crowd=False, palette=None):
    runs, pixels = bitmap_to_runs(bitmap)
    return InstanceRecord(
        instance_id=instance_id,
        scene_id="scene-x",
        semantic_class=semantic,
        width=bitmap.shape[1],
        height=bitmap.shape[0],
        runs=runs,
        source_version=source,
        confidence=confidence,
        is_crowd=crowd,
        palette_name=palette[0] if palette else None,
        palette_rgb=palette[1] if palette else None,
        pixel_count=pixels,
    )


def _config(**overrides):
    base = dict(
        workers=1,
        rle_compression=1,
        retention_policy="keep_all",
        min_coverage=0.01,
        iou_threshold=0.3,
        default_confidence=0.5,
        default_crowd=False,
        snapshot_commit_interval=1,
        fault_injection={},
    )
    base.update(overrides)
    return PipelineConfig(**base)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_upgrade_infers_defaults_and_palette(self):
        bitmap = np.zeros((3, 3), dtype=np.uint8)
        bitmap[0:2, 0:2] = 1
        record = _record("a-0", V1, bitmap, confidence=0.5)
        record = InstanceRecord(**{**record.to_json_dict(), "source_flags": {"confidence_inferred": True, "crowd_inferred": True}})
        config = _config()
        losses = upgrade_v1_to_v2([record], self.root / "up.json", config)
        _width, _height, parsed, palette = parse_v2_package(self.root / "up.json")
        self.assertEqual(parsed[0].confidence, 0.5)
        self.assertFalse(parsed[0].is_crowd)
        self.assertIn(parsed[0].palette_name, palette)
        self.assertEqual({loss.field_name for loss in losses}, {"confidence", "is_crowd"})

    def test_v2_downgrade_records_all_losses_and_roundtrips_runs(self):
        bitmap = np.zeros((3, 3), dtype=np.uint8)
        bitmap[1, 1] = 1
        record = _record("b-0", V2, bitmap, palette=("color_car", (1, 2, 3)))
        config = _config(rle_compression=0)
        losses = downgrade_v2_to_v1([record], self.root / "down.bin", config)
        _width, _height, parsed = parse_v1_package(self.root / "down.bin")
        self.assertEqual(parsed[0].runs, record.runs)
        self.assertEqual(parsed[0].instance_id, record.instance_id)
        fields = {loss.field_name for loss in losses}
        self.assertEqual(fields, {"confidence", "is_crowd", "palette"})


class FusionTests(unittest.TestCase):
    def test_matching_merge_and_unique_ownership(self):
        bitmap_a = np.zeros((4, 4), dtype=np.uint8)
        bitmap_b = np.zeros((4, 4), dtype=np.uint8)
        bitmap_a[0:3, 0:3] = 1
        bitmap_b[0:3, 1:4] = 1
        records = [
            _record("a-0", V1, bitmap_a, confidence=0.8),
            _record("b-0", V2, bitmap_b, confidence=0.7),
        ]
        result = fuse_scene("scene-x", records, 0.3, 0.01, "keep_all")
        self.assertEqual(len(result.fused_instances), 1)
        fused = result.fused_instances[0]
        self.assertEqual(fused.contributors, ["a-0", "b-0"])
        # Every covered pixel has exactly one owner; labels stay unique.
        covered = int(np.count_nonzero(bitmap_a | bitmap_b))
        self.assertEqual(int(np.count_nonzero(result.label_map)), covered)
        self.assertEqual(set(np.unique(result.label_map)) - {0}, {1})

    def test_crowd_covers_but_never_competes(self):
        base = np.zeros((4, 4), dtype=np.uint8)
        base[0:2, 0:2] = 1
        crowd = np.zeros((4, 4), dtype=np.uint8)
        crowd[1:4, 1:4] = 1
        records = [
            _record("a-0", V1, base, confidence=0.9),
            _record("c-0", V2, crowd, confidence=0.99, crowd=True),
        ]
        result = fuse_scene("scene-x", records, 0.3, 0.01, "keep_all")
        self.assertEqual(len(result.fused_instances), 2)
        crowd_fused = next(item for item in result.fused_instances if item.is_crowd)
        self.assertEqual(crowd_fused.contributors, ["c-0"])
        # Non-crowd wins disputed pixels.
        non_crowd = next(item for item in result.fused_instances if not item.is_crowd)
        expected_label = int(non_crowd.fused_id.split("-")[1])
        # The disputed pixel keeps the non-crowd owner even though crowd has
        # the higher confidence.
        self.assertEqual(int(result.label_map[1, 1]), expected_label)

    def test_retention_policy_drops_small_instances(self):
        bitmap = np.zeros((10, 10), dtype=np.uint8)
        bitmap[0, 0] = 1
        records = [_record("s-0", V2, bitmap)]
        kept = fuse_scene("scene-x", records, 0.3, 0.02, "drop_below_coverage")
        self.assertEqual(kept.fused_instances, [])
        self.assertIn("s-0", kept.dropped_instances)
        all_kept = fuse_scene("scene-x", records, 0.3, 0.01, "drop_below_coverage")
        self.assertEqual(len(all_kept.fused_instances), 1)

    def test_conflict_tie_break_ordering_is_deterministic(self):
        bitmap_a = np.zeros((4, 4), dtype=np.uint8)
        bitmap_b = np.zeros((4, 4), dtype=np.uint8)
        bitmap_a[:, :] = 1
        bitmap_b[:, :] = 1
        records = [
            _record("z-9", V1, bitmap_a, confidence=0.5),
            _record("a-1", V2, bitmap_b, confidence=0.5),
        ]
        result = fuse_scene("scene-x", records, 0.3, 0.01, "keep_all")
        self.assertEqual(len(result.fused_instances), 1)
        # Contributor list keeps the v1,v2 slot order; the lexicographic tie
        # break is exercised inside the weighted merge, not by re-sorting ids.
        self.assertEqual(result.fused_instances[0].contributors, ["z-9", "a-1"])
        # Identical masks and equal confidence: smaller id a-1 wins every
        # shared pixel, so the merged bitmap retains the full union.
        self.assertEqual(result.fused_instances[0].pixel_count, 16)
        self.assertEqual(int(np.count_nonzero(result.label_map)), 16)


if __name__ == "__main__":
    unittest.main()
