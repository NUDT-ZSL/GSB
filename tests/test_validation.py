from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from grayroll.errors import GrayrollError
from grayroll.samples import generate_samples, sample_v1, sample_v2_shards
from grayroll.sources import load_package
from grayroll.util import sha256_file, write_json_file
from tests.helpers import copy_samples, run_cli, variant_package


class ValidationTests(unittest.TestCase):
    def test_sample_generation_is_byte_reproducible(self) -> None:
        def tree_hashes(root: Path) -> dict[str, str]:
            return {
                str(path.relative_to(root)): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            first_root = Path(first) / "samples"
            second_root = Path(second) / "samples"
            generate_samples(first_root)
            generate_samples(second_root)
            self.assertEqual(tree_hashes(first_root), tree_hashes(second_root))

    def test_invalid_v1_tiers_return_bad_rhythm_without_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            v1 = sample_v1()
            v1["batches"]["1"]["tiers"] = [0.1, 0.5, 0.9]
            write_json_file(sample_root / "v1" / "rhythm.json", v1)
            manifest = json.loads((sample_root / "manifest.json").read_text("utf-8"))
            manifest["v1"]["sha256"] = sha256_file(sample_root / "v1" / "rhythm.json")
            write_json_file(sample_root / "manifest.json", manifest)
            state = root / "state.db"
            result = run_cli(
                [
                    "load",
                    "--manifest",
                    str(sample_root / "manifest.json"),
                    "--defaults",
                    str(sample_root / "defaults.json"),
                ],
                state,
            )
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "E_BAD_RHYTHM")
            self.assertFalse(state.exists())

    def test_bad_shard_hash_returns_bad_shard_without_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            shard = sample_root / "v2" / "shards" / "batch-1.json"
            shard.write_bytes(shard.read_bytes() + b"\n")
            state = root / "state.db"
            result = run_cli(
                [
                    "load",
                    "--manifest",
                    str(sample_root / "manifest.json"),
                    "--defaults",
                    str(sample_root / "defaults.json"),
                ],
                state,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "E_BAD_SHARD")
            self.assertFalse(state.exists())

    def test_v2_metadata_shard_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            meta_path = sample_root / "v2" / "meta.db"
            connection = sqlite3.connect(meta_path)
            try:
                connection.execute(
                    "UPDATE shards SET criteria_json = ? WHERE batch_id = '1'",
                    ('{"min_samples":99,"rules":[],"unknown_metric":"FAIL"}',),
                )
                connection.commit()
            finally:
                connection.close()
            manifest = json.loads((sample_root / "manifest.json").read_text("utf-8"))
            manifest["v2_meta"]["meta_sha256"] = sha256_file(meta_path)
            write_json_file(sample_root / "manifest.json", manifest)
            with self.assertRaises(GrayrollError) as raised:
                load_package(
                    sample_root / "manifest.json", sample_root / "defaults.json"
                )
            self.assertEqual(raised.exception.code, "E_BAD_SHARD")

    def test_undeclared_dependency_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shards = sample_v2_shards()
            shards["v2/shards/batch-4.json"]["depends_on"] = ["99"]
            sample_root = variant_package(root, shards=shards)
            state = root / "state.db"
            result = run_cli(
                [
                    "load",
                    "--manifest",
                    str(sample_root / "manifest.json"),
                    "--defaults",
                    str(sample_root / "defaults.json"),
                ],
                state,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "E_BAD_SHARD")
            self.assertFalse(state.exists())

    def test_invalid_rollback_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shards = sample_v2_shards()
            shards["v2/shards/batch-2.json"]["rollback_window"] = {
                "start_ms": 100,
                "end_ms": 99,
            }
            sample_root = variant_package(root, shards=shards)
            with self.assertRaises(GrayrollError) as raised:
                load_package(
                    sample_root / "manifest.json", sample_root / "defaults.json"
                )
            self.assertEqual(raised.exception.code, "E_BAD_SHARD")

    def test_duplicate_json_batch_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample_root = copy_samples(root)
            batch = sample_v1()["batches"]["1"]
            document = (
                '{"batches":{"1":'
                + json.dumps(batch, sort_keys=True)
                + ',"1":'
                + json.dumps(batch, sort_keys=True)
                + '},"format":"rhythm-v1","logical_clock_ms":1699999999000,'
                + '"service":"checkout.svc"}'
            )
            (sample_root / "v1" / "rhythm.json").write_text(document, "utf-8")
            manifest = json.loads((sample_root / "manifest.json").read_text("utf-8"))
            manifest["v1"]["sha256"] = sha256_file(sample_root / "v1" / "rhythm.json")
            write_json_file(sample_root / "manifest.json", manifest)
            with self.assertRaises(GrayrollError) as raised:
                load_package(
                    sample_root / "manifest.json", sample_root / "defaults.json"
                )
            self.assertEqual(raised.exception.code, "E_BAD_RHYTHM")


if __name__ == "__main__":
    unittest.main()
