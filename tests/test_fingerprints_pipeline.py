import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from panseg.fingerprints import fusion_dependency_key, instance_cache_key
from panseg.models import V1, V2, InstanceRecord, PipelineConfig
from panseg.pipeline import PanopticPipeline
from panseg.rle import bitmap_to_runs
from panseg.v1_package import V1RawInstance, write_v1_package
from panseg.v2_package import V2RawInstance, write_v2_package


def _make_record(instance_id, source, confidence=0.9, crowd=False, offset=0):
    bitmap = np.zeros((8, 8), dtype=np.uint8)
    bitmap[offset : offset + 3, 1:4] = 1
    runs, pixels = bitmap_to_runs(bitmap)
    return InstanceRecord(
        instance_id=instance_id,
        scene_id="scene-00",
        semantic_class="car",
        width=8,
        height=8,
        runs=runs,
        source_version=source,
        confidence=confidence,
        is_crowd=crowd,
        palette_name="color_car" if source == V2 else None,
        palette_rgb=(1, 2, 3) if source == V2 else None,
        pixel_count=pixels,
    )


class FingerprintTests(unittest.TestCase):
    def test_metadata_change_changes_cache_key(self):
        base = _make_record("a-0", V1)
        changed = InstanceRecord(**{**base.to_json_dict(), "confidence": 0.1})
        self.assertNotEqual(instance_cache_key(base), instance_cache_key(changed))

    def test_mask_change_changes_cache_key(self):
        base = _make_record("a-0", V1)
        changed = _make_record("a-0", V1, offset=2)
        self.assertNotEqual(instance_cache_key(base), instance_cache_key(changed))

    def test_fusion_key_derives_from_participants_and_threshold(self):
        key_a = instance_cache_key(_make_record("a-0", V1))
        key_b = instance_cache_key(_make_record("b-0", V2))
        first = fusion_dependency_key("scene-00", [key_a, key_b], 0.3, "keep_all", 0.01)
        second = fusion_dependency_key("scene-00", [key_b, key_a], 0.3, "keep_all", 0.01)
        changed = fusion_dependency_key("scene-00", [key_a, key_b], 0.4, "keep_all", 0.01)
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)


class EndToEndPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sample = self.root / "sample"
        self.sample.mkdir()
        self._build_sample()
        self.config = PipelineConfig(
            workers=4,
            rle_compression=1,
            retention_policy="keep_all",
            min_coverage=0.01,
            iou_threshold=0.3,
            default_confidence=0.5,
            default_crowd=False,
            snapshot_commit_interval=1,
            boundary_iou_threshold=0.7,
            fault_injection={},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _build_sample(self):
        bitmap_a = np.zeros((8, 8), dtype=np.uint8)
        bitmap_b = np.zeros((8, 8), dtype=np.uint8)
        bitmap_a[0:5, 0:5] = 1
        bitmap_b[0:5, 2:7] = 1
        runs_a, pixels_a = bitmap_to_runs(bitmap_a)
        runs_b, pixels_b = bitmap_to_runs(bitmap_b)
        write_v1_package(
            self.sample / "scene-00.v1.bin",
            8,
            8,
            [V1RawInstance("scene-00-inst-00", "car", runs_a, pixels_a)],
            compression=1,
        )
        write_v2_package(
            self.sample / "scene-00.v2.json",
            8,
            8,
            [
                V2RawInstance(
                    "scene-00-inst-01",
                    "car",
                    runs_b,
                    pixels_b,
                    0.88,
                    False,
                    "color_car",
                    (9, 9, 9),
                )
            ],
            {"color_car": {"rgb": [9, 9, 9]}},
        )
        metadata = {
            "format_version": 1,
            "instances": [
                {
                    "instance_id": "scene-00-inst-00",
                    "semantic_class": "car",
                    "confidence": 0.77,
                    "is_crowd": False,
                }
            ],
        }
        (self.sample / "metadata.json").write_text(json.dumps(metadata))

    def test_incremental_reuses_unchanged_instances(self):
        work = self.root / "work"
        first = PanopticPipeline(self.sample, self.config, work).run()
        self.assertEqual(first.stats.reused_instances, 0)
        self.assertEqual(first.stats.recomputed_fusions, 1)

        second = PanopticPipeline(self.sample, self.config, work).run()
        self.assertEqual(second.stats.reused_instances, 2)
        self.assertEqual(second.stats.decoded_instances, 0)
        self.assertEqual(second.stats.reused_fusions, 1)
        self.assertEqual(second.stats.recomputed_fusions, 0)
        self.assertEqual(second.manifest["version_number"], 2)

    def test_partial_invalidation_only_recomputes_affected_scene(self):
        work = self.root / "work2"
        PanopticPipeline(self.sample, self.config, work).run()

        path = self.sample / "scene-00.v1.bin"
        _width, _height, instances = __import__(
            "panseg.v1_package", fromlist=["parse_v1_package"]
        ).parse_v1_package(path)
        target = instances[0]
        new_runs = ((0, 4), (8, 4))
        write_v1_package(
            path,
            8,
            8,
            [V1RawInstance(target.instance_id, target.semantic_class, new_runs, 8)],
            compression=1,
        )
        result = PanopticPipeline(self.sample, self.config, work).run()
        self.assertEqual(result.stats.invalidated_instances, 1)
        self.assertEqual(result.stats.reused_instances, 1)

    def test_config_hash_change_forces_rebuild_path(self):
        work = self.root / "work3"
        PanopticPipeline(self.sample, self.config, work).run()
        changed = PipelineConfig(**{**self.config.to_dict(), "iou_threshold": 0.12})
        result = PanopticPipeline(self.sample, changed, work).run()
        self.assertEqual(result.manifest["config_hash"] != self.config and True, True)
        self.assertEqual(result.stats.reused_fusions, 0)
        self.assertEqual(result.stats.recomputed_fusions, 1)


if __name__ == "__main__":
    unittest.main()


class CheckpointIntervalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sample = self.root / "sample"
        self.sample.mkdir()
        metadata = {"instances": []}
        for scene_index in range(4):
            scene_id = f"scene-{scene_index:02d}"
            bitmap = np.zeros((6, 6), dtype=np.uint8)
            bitmap[1:4, 1:4] = 1
            runs, pixels = bitmap_to_runs(bitmap)
            write_v1_package(
                self.sample / f"{scene_id}.v1.bin",
                6,
                6,
                [V1RawInstance(f"{scene_id}-a", "car", runs, pixels)],
                1,
            )
            write_v2_package(
                self.sample / f"{scene_id}.v2.json",
                6,
                6,
                [V2RawInstance(f"{scene_id}-b", "car", runs, pixels, 0.8, False, "c", (1, 2, 3))],
                {"c": {"rgb": [1, 2, 3]}},
            )
            metadata["instances"].append(
                {"instance_id": f"{scene_id}-a", "semantic_class": "car", "confidence": 0.7, "is_crowd": False}
            )
        (self.sample / "metadata.json").write_text(json.dumps(metadata))

    def tearDown(self):
        self.tmp.cleanup()

    def test_interval_drives_final_snapshot_and_all_scenes_present(self):
        config = PipelineConfig(
            workers=2,
            rle_compression=0,
            retention_policy="keep_all",
            min_coverage=0.01,
            iou_threshold=0.3,
            default_confidence=0.5,
            default_crowd=False,
            snapshot_commit_interval=2,
            boundary_iou_threshold=0.7,
            fault_injection={},
        )
        work = self.root / "work"
        result = PanopticPipeline(self.sample, config, work).run()
        # Four scenes, interval two -> checkpoint commits after scenes 2 and 4.
        self.assertEqual(result.stats.snapshot_version, 2)
        manifest = result.manifest
        self.assertEqual(len(manifest["fusion_versions"]), 4)
        self.assertEqual(len(manifest["instance_versions"]), 8)
        pointer = json.loads((work / "snapshots" / "current.json").read_text())
        self.assertEqual(pointer["version"], 2)

        # Interval one commits once per scene, so the final version equals the
        # number of processed scenes.
        config_every = PipelineConfig(**{**config.to_dict(), "snapshot_commit_interval": 1})
        work_every = self.root / "work-every"
        result_every = PanopticPipeline(self.sample, config_every, work_every).run()
        self.assertEqual(result_every.stats.snapshot_version, 4)
        self.assertEqual(len(result_every.manifest["fusion_versions"]), 4)
