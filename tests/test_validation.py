from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qos_pipeline.errors import QosError
from qos_pipeline.sample import load_sample
from qos_pipeline.serialization import canonical_json
from qos_pipeline.store import Store

from .helpers import ROOT, fresh_sample_documents, write_sample_directory


class ValidationTests(unittest.TestCase):
    def assert_error(self, code: str, manifest: Path) -> None:
        with self.assertRaises(QosError) as context:
            load_sample(manifest)
        self.assertEqual(context.exception.code, code)

    def test_fixed_sample_loads_and_digest_is_repeatable(self) -> None:
        first = load_sample(ROOT / "samples" / "manifest.json")
        second = load_sample(ROOT / "samples" / "manifest.json")
        self.assertEqual(first.sample_digest, second.sample_digest)
        self.assertEqual(len(first.events), 236)
        self.assertEqual(len(first.classes), 4)

    def test_duplicate_class_is_rejected(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        config["classes"].append(dict(config["classes"][1]))
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_CLASS_DUPLICATE", path)

    def test_peak_burst_must_not_be_smaller(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        config["classes"][1]["peak_burst_bytes"] = (
            config["classes"][1]["committed_burst_bytes"] - 1
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_PEAK_BURST", path)

    def test_guarantee_sum_is_bounded_by_parent_cir(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        config["classes"][1]["guarantee_bps"] = 9_000_000
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_GUARANTEE_SUM", path)

    def test_hierarchy_cycle_is_rejected(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        config["classes"][0]["parent"] = "interactive"
        config["classes"][0]["guarantee_bps"] = 1_000_000
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_HIERARCHY_CYCLE", path)

    def test_event_sequence_must_increase(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        arrivals["events"][1]["seq"] = arrivals["events"][0]["seq"]
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_EVENT_SEQ", path)

    def test_arrival_time_must_be_non_negative(self) -> None:
        manifest, config, arrivals = fresh_sample_documents()
        arrivals["events"][0]["arrival_ms"] = -1
        with tempfile.TemporaryDirectory() as temporary:
            path = write_sample_directory(Path(temporary), config, arrivals, manifest)
            self.assert_error("E_EVENT_TIME", path)

    def test_invalid_sample_does_not_mutate_loaded_database(self) -> None:
        valid = load_sample(ROOT / "samples" / "manifest.json")
        manifest, config, arrivals = fresh_sample_documents()
        config["classes"][1]["cir_bps"] = 0
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            invalid_manifest = write_sample_directory(
                temporary_path, config, arrivals, manifest
            )
            store = Store(temporary_path / "state.db")
            store.initialize(valid)
            before = canonical_json(store.status())
            with self.assertRaises(QosError) as context:
                load_sample(invalid_manifest)
            self.assertEqual(context.exception.code, "E_CLASS_PARAM")
            after = canonical_json(store.status())
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()

