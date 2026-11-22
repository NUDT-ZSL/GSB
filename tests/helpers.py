from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from motion_pipeline.config import PipelineConfig
from motion_pipeline.dataset import generate_dataset
from motion_pipeline.deterministic import canonical_bytes
from motion_pipeline.errors import BusinessError
from motion_pipeline.pipeline import Pipeline


def read_jsonl(path: Path) -> list[Any]:
    with path.open("rb") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, records: list[Any]) -> None:
    data = b"".join(canonical_bytes(record) + b"\n" for record in records)
    path.write_bytes(data)


class DatasetTestCase(unittest.TestCase):
    clips: int = 3
    seed: int = 101

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="motion-pipeline-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset_dir = self.root / "dataset"
        generate_dataset(
            self.dataset_dir, seed=self.seed, clip_count=self.clips
        )
        self.manifest = self.dataset_dir / "manifest.jsonl"
        self.frames = self.dataset_dir / "frames.jsonl"

    def assert_business_error(
        self,
        code: str,
        callable_: Callable[[], Any],
        *,
        field: str | None = None,
        has_clip_id: bool = False,
    ) -> BusinessError:
        with self.assertRaises(BusinessError) as caught:
            callable_()
        error = caught.exception
        self.assertEqual(error.code, code)
        if field is not None:
            self.assertEqual(error.field, field)
        if has_clip_id:
            self.assertIsNotNone(error.clip_id)
        return error

    def run_pipeline(
        self,
        workdir: str | Path,
        config: PipelineConfig | None = None,
        **kwargs: Any,
    ):
        return Pipeline(Path(workdir), config).run(
            self.manifest, self.frames, **kwargs
        )
