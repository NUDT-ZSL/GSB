from __future__ import annotations

import copy
import unittest

from hotgray.errors import BizError, ErrorCode
from hotgray.models import load_samples

from .support import SAMPLES, TempWorkspace, copy_samples, mutate_json, new_engine


class ValidationTests(unittest.TestCase):
    def test_valid_sample_manifest(self) -> None:
        samples = load_samples(SAMPLES)
        self.assertEqual(sorted(samples.manifest.models), [1, 2])
        self.assertEqual([stage.ratio for stage in samples.plan.stages], [0.1, 0.5, 1.0])
        self.assertEqual(len(samples.patch.entries), 2)
        self.assertEqual(
            {entry.layer for entry in samples.patch.entries},
            {"encoder.1", "head"},
        )

    def _assert_init_failure(self, mutate, expected_code: ErrorCode) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)
            mutate(samples)
            db_path = workspace.path / "state.db"
            with self.assertRaises(BizError) as raised:
                new_engine(workspace.path, samples)
            self.assertEqual(raised.exception.code, expected_code)
            self.assertFalse(db_path.exists(), "非法输入不得留下持久化数据库")

    def test_version_must_be_monotonic(self) -> None:
        def mutate(samples):
            path = samples / "models.json"
            mutate_json(path, lambda value: {**value, "models": list(reversed(value["models"]))})

        self._assert_init_failure(mutate, ErrorCode.VERSION_NOT_MONOTONIC)

    def test_layer_names_must_be_unique(self) -> None:
        def mutate(samples):
            path = samples / "models.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["models"][1]["layers"].append(copy.deepcopy(copied["models"][1]["layers"][0]))
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.DUPLICATE_LAYER)

    def test_param_count_must_be_positive_integer(self) -> None:
        def mutate(samples):
            path = samples / "models.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["models"][1]["layers"][0]["param_count"] = 0
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.INVALID_PARAM_COUNT)

    def test_shards_must_cover_all_layer_bytes(self) -> None:
        def mutate(samples):
            path = samples / "models.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["models"][1]["layers"][1]["shards"][-1]["byte_end"] -= 4
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.MISSING_SHARD)

    def test_config_reference_must_resolve(self) -> None:
        def mutate(samples):
            path = samples / "models.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["models"][1]["preprocess"] = "pre:missing"
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.UNRESOLVED_REFERENCE)

    def test_config_reference_cycle_is_rejected(self) -> None:
        def mutate(samples):
            path = samples / "models.json"

            def change(value):
                copied = copy.deepcopy(value)
                for resource in copied["resources"]:
                    if resource["id"] == "upstream:v1":
                        resource["depends_on"] = ["pre:v1"]
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.CONFIG_REFERENCE_CYCLE)

    def test_gray_ratios_must_be_strictly_increasing_and_end_at_one(self) -> None:
        def invalid_final(samples):
            path = samples / "plan.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["stages"][-1]["ratio"] = 0.9
                return copied

            mutate_json(path, change)

        self._assert_init_failure(invalid_final, ErrorCode.INVALID_GRAY_RATIO)

        def non_increasing(samples):
            path = samples / "plan.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["stages"][1]["ratio"] = copied["stages"][0]["ratio"]
                return copied

            mutate_json(path, change)

        self._assert_init_failure(non_increasing, ErrorCode.INVALID_GRAY_RATIO)

    def test_patch_missing_layer_has_distinct_error(self) -> None:
        def mutate(samples):
            path = samples / "patches" / "v1_to_v2.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["entries"][0]["layer"] = "ghost.layer"
                return copied

            mutate_json(path, change)

        self._assert_init_failure(mutate, ErrorCode.MISSING_LAYER_REFERENCE)

    def test_patch_missing_shard_has_distinct_error_and_fields(self) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)
            path = samples / "patches" / "v1_to_v2.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["entries"][0]["shard_index"] = 99
                return copied

            mutate_json(path, change)
            with self.assertRaises(BizError) as raised:
                new_engine(workspace.path, samples)
            error = raised.exception
            self.assertEqual(error.code, ErrorCode.MISSING_SHARD)
            self.assertIn("layer", error.details)
            self.assertIn("shard_index", error.details)
            self.assertFalse((workspace.path / "state.db").exists())

    def test_patch_digest_mismatch_has_distinct_error_and_fields(self) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)
            path = samples / "patches" / "v1_to_v2.json"

            def change(value):
                copied = copy.deepcopy(value)
                copied["entries"][0]["digest"] = "sha256:" + "0" * 64
                return copied

            mutate_json(path, change)
            with self.assertRaises(BizError) as raised:
                new_engine(workspace.path, samples)
            error = raised.exception
            self.assertEqual(error.code, ErrorCode.SHARD_DIGEST_MISMATCH)
            self.assertEqual(error.details["version"], 2)
            self.assertIn(error.details["layer"], {"encoder.1", "head"})
            self.assertIn(error.details["shard_index"], {0, 1})
            self.assertFalse((workspace.path / "state.db").exists())

    def test_missing_shard_file_is_rejected_before_database_creation(self) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)
            target = samples / "weights" / "v2" / "head" / "shard-000.bin"
            target.unlink()
            with self.assertRaises(BizError) as raised:
                new_engine(workspace.path, samples)
            self.assertEqual(raised.exception.code, ErrorCode.MISSING_SHARD)
            self.assertEqual(raised.exception.details.get("layer"), "head")
            self.assertEqual(raised.exception.details.get("shard_index"), 0)
            self.assertFalse((workspace.path / "state.db").exists())

    def test_plan_members_must_load_declared_baseline(self) -> None:
        with TempWorkspace() as workspace:
            samples = copy_samples(workspace.path)

            def change(value):
                copied = copy.deepcopy(value)
                for replica in copied:
                    if replica["replica_id"] == "replica-a":
                        replica["loaded_version"] = 2
                        replica["loaded_shards"] = {
                            layer.name: [shard.digest for shard in layer.shards]
                            for layer in load_samples(samples).manifest.model(2).layers
                        }
                return copied

            mutate_json(samples / "replicas.json", change)
            with self.assertRaises(BizError) as raised:
                new_engine(workspace.path, samples)
            self.assertEqual(raised.exception.code, ErrorCode.REPLICA_INVALID)
            self.assertEqual(raised.exception.details["replica_id"], "replica-a")
            self.assertFalse((workspace.path / "state.db").exists())


if __name__ == "__main__":
    unittest.main()
