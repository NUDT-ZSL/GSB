from __future__ import annotations

import hashlib
import json

from pointcloud_workbench.samplegen import generate_samples
from pointcloud_workbench.spec import load_edit_manifest, load_task

from tests.helpers import SampleTest


class SampleGeneratorTests(SampleTest):
    def test_fixed_seed_generation_is_byte_deterministic(self) -> None:
        first = self.samples
        second = self.root / "samples_second"
        generate_samples(second)
        first_files = sorted(path.name for path in first.iterdir())
        second_files = sorted(path.name for path in second.iterdir())
        self.assertEqual(first_files, second_files)
        for name in first_files:
            self.assertEqual(
                hashlib.sha256((first / name).read_bytes()).hexdigest(),
                hashlib.sha256((second / name).read_bytes()).hexdigest(),
                name,
            )

    def test_edit_manifest_validates_region_digests(self) -> None:
        task = load_task(self.samples / "task_edited.json")
        edit = load_edit_manifest(task.edit_manifest_path, task)
        self.assertEqual(len(edit.regions), 3)
        self.assertEqual(edit.affected_indices.size, 92)
        self.assertEqual(edit.base_dataset_digest, self.manifest["base_dataset_digest"])
        self.assertEqual(edit.edited_dataset_digest, self.manifest["edited_dataset_digest"])

    def test_all_sample_inputs_are_local_json_or_binary(self) -> None:
        manifest = json.loads((self.samples / "sample_manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("http", json.dumps(manifest).lower())
        for task_name in ("task_base.json", "task_edited.json"):
            document = json.loads((self.samples / task_name).read_text(encoding="utf-8"))
            self.assertFalse(document["pointcloud"]["path"].startswith(("http", "/")))

