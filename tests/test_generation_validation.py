from __future__ import annotations

import copy
import hashlib

from motion_pipeline.config import PipelineConfig
from motion_pipeline.dataset import generate_dataset, load_dataset
from motion_pipeline.errors import BusinessError

from tests.helpers import DatasetTestCase, read_jsonl, write_jsonl


class GenerationTests(DatasetTestCase):
    def test_generation_is_byte_deterministic(self) -> None:
        other = self.root / "other"
        generate_dataset(other, seed=self.seed, clip_count=self.clips)
        for name in ("manifest.jsonl", "frames.jsonl", "dataset_meta.json"):
            self.assertEqual(
                (self.dataset_dir / name).read_bytes(),
                (other / name).read_bytes(),
                msg=name,
            )
        self.assertEqual(
            hashlib.sha256((self.dataset_dir / "frames.jsonl").read_bytes()).hexdigest(),
            hashlib.sha256((other / "frames.jsonl").read_bytes()).hexdigest(),
        )

    def test_generated_manifest_has_required_controlled_fields(self) -> None:
        records = read_jsonl(self.manifest)
        self.assertEqual(len(records), self.clips)
        for record in records:
            self.assertEqual(
                set(record),
                {
                    "clip_id",
                    "frame_count",
                    "frame_interval_ms",
                    "object_count",
                    "motion_mode",
                    "seed",
                    "temporal_policy",
                },
            )
            self.assertIn(record["motion_mode"], {"constant", "accelerated", "periodic"})
            self.assertIn(
                record["temporal_policy"], {"every_frame", "stride_2", "stride_3"}
            )


class ValidationTests(DatasetTestCase):
    def _mutated_manifest(self, mutator, name: str):
        records = copy.deepcopy(read_jsonl(self.manifest))
        mutator(records)
        path = self.root / name
        write_jsonl(path, records)
        return path

    def _mutated_frames(self, mutator, name: str):
        records = copy.deepcopy(read_jsonl(self.frames))
        mutator(records)
        path = self.root / name
        write_jsonl(path, records)
        return path

    def test_duplicate_clip_id(self) -> None:
        manifest = self._mutated_manifest(
            lambda records: records.append(copy.deepcopy(records[0])),
            "duplicate.jsonl",
        )
        error = self.assert_business_error(
            "E_DUPLICATE_CLIP_ID",
            lambda: load_dataset(manifest, self.frames, PipelineConfig()),
            field="clip_id",
            has_clip_id=True,
        )
        self.assertEqual(error.clip_id, read_jsonl(manifest)[0]["clip_id"])

    def test_frame_count_mismatch(self) -> None:
        manifest = self._mutated_manifest(
            lambda records: records[0].__setitem__(
                "frame_count", records[0]["frame_count"] + 1
            ),
            "frame-count.jsonl",
        )
        self.assert_business_error(
            "E_FRAME_COUNT_MISMATCH",
            lambda: load_dataset(manifest, self.frames, PipelineConfig()),
            field="frame_count",
            has_clip_id=True,
        )

    def test_mask_shape_mismatch(self) -> None:
        frames = self._mutated_frames(
            lambda records: records[0]["frames"][0].__setitem__(
                "shape", [33, 32]
            ),
            "mask-shape.jsonl",
        )
        self.assert_business_error(
            "E_MASK_SHAPE_MISMATCH",
            lambda: load_dataset(self.manifest, frames, PipelineConfig()),
            field="mask.shape",
            has_clip_id=True,
        )

    def test_unknown_temporal_policy(self) -> None:
        manifest = self._mutated_manifest(
            lambda records: records[0].__setitem__(
                "temporal_policy", "unknown_policy"
            ),
            "policy.jsonl",
        )
        self.assert_business_error(
            "E_TEMPORAL_POLICY_INVALID",
            lambda: load_dataset(manifest, self.frames, PipelineConfig()),
            field="temporal_policy",
            has_clip_id=True,
        )

    def test_nonpositive_frame_interval(self) -> None:
        manifest = self._mutated_manifest(
            lambda records: records[0].__setitem__("frame_interval_ms", 0),
            "interval.jsonl",
        )
        self.assert_business_error(
            "E_FRAME_INTERVAL_INVALID",
            lambda: load_dataset(manifest, self.frames, PipelineConfig()),
            field="frame_interval_ms",
            has_clip_id=True,
        )

    def test_unknown_motion_mode(self) -> None:
        manifest = self._mutated_manifest(
            lambda records: records[0].__setitem__("motion_mode", "warp"),
            "motion.jsonl",
        )
        self.assert_business_error(
            "E_MOTION_MODE_INVALID",
            lambda: load_dataset(manifest, self.frames, PipelineConfig()),
            field="motion_mode",
            has_clip_id=True,
        )

    def test_window_and_decay_errors_include_clip_id(self) -> None:
        first_clip = read_jsonl(self.manifest)[0]["clip_id"]
        error = self.assert_business_error(
            "E_WINDOW_INVALID",
            lambda: load_dataset(
                self.manifest,
                self.frames,
                PipelineConfig(history_window_length=0),
            ),
            field="history_window_length",
            has_clip_id=True,
        )
        self.assertEqual(error.clip_id, first_clip)
        error = self.assert_business_error(
            "E_DECAY_INVALID",
            lambda: load_dataset(
                self.manifest, self.frames, PipelineConfig(decay=0.0)
            ),
            field="decay",
            has_clip_id=True,
        )
        self.assertEqual(error.clip_id, first_clip)
