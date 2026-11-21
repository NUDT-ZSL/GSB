from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scheduler_diag.errors import BusinessError
from scheduler_diag.loader import load_bundle
from scheduler_diag.models import FILTER_PLUGINS
from scheduler_diag.service import load_workspace

from tests.helpers import sample_copy, write_bundle


class ValidationTests(unittest.TestCase):
    def _load_mutated(self, mutate) -> BusinessError:
        bundle = sample_copy()
        mutate(bundle)
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_bundle(Path(temporary) / "bundle", bundle)
            with self.assertRaises(BusinessError) as caught:
                load_bundle(directory)
        return caught.exception

    def test_valid_sample_and_filter_order_normalization(self) -> None:
        bundle_data = sample_copy()
        bundle_data["config"]["filter_plugins"] = list(
            reversed(bundle_data["config"]["filter_plugins"])
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = write_bundle(Path(temporary) / "bundle", bundle_data)
            bundle = load_bundle(directory)
        self.assertEqual(bundle.config.filter_plugins, FILTER_PLUGINS)
        self.assertEqual(bundle.max_round, 4)
        self.assertEqual(len(bundle.injections), 4)

    def test_duplicate_node_name(self) -> None:
        def mutate(bundle):
            bundle["nodes"][1]["name"] = bundle["nodes"][0]["name"]

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_NODE")
        self.assertIn("unique", error.message)

    def test_bad_node_integer(self) -> None:
        def mutate(bundle):
            bundle["nodes"][0]["cpu_millicores"] = 0

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_NODE")
        self.assertIn("cpu_millicores", error.message)

    def test_bad_taint_format(self) -> None:
        def mutate(bundle):
            bundle["nodes"][1]["taints"][0]["key"] = "bad key!"

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_NODE")

    def test_bad_pod_float_resource(self) -> None:
        def mutate(bundle):
            bundle["pods"][0]["memory_mib"] = 512.5

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_POD")
        self.assertIn("memory_mib", error.message)

    def test_toleration_value_required(self) -> None:
        def mutate(bundle):
            bundle["pods"][2]["tolerations"][0]["value"] = None

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_POD")
        self.assertIn("value", error.message)

    def test_missing_filter_plugin(self) -> None:
        def mutate(bundle):
            bundle["config"]["filter_plugins"].pop()

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_CONFIG")

    def test_nonpositive_weight_sum(self) -> None:
        def mutate(bundle):
            for key in bundle["config"]["score_weights"]:
                bundle["config"]["score_weights"][key] = 0

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_CONFIG")
        self.assertIn("positive", error.message)

    def test_injection_wrong_round(self) -> None:
        def mutate(bundle):
            bundle["injections"][0]["round"] = 3

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_INJECT")
        self.assertIn("arrival round", error.message)

    def test_injection_wrong_phase(self) -> None:
        def mutate(bundle):
            bundle["injections"][0]["phase"] = "score"

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_INJECT")

    def test_missing_topology_label_on_node(self) -> None:
        def mutate(bundle):
            bundle["pods"][0]["topology_spread"][0]["topology_key"] = "datacenter"

        error = self._load_mutated(mutate)
        self.assertEqual(error.code, "E_BAD_POD")
        self.assertIn("topology", error.message)

    def test_invalid_load_does_not_create_database(self) -> None:
        bundle_data = sample_copy()
        bundle_data["nodes"][0]["cpu_millicores"] = -1
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = write_bundle(root / "bad-bundle", bundle_data)
            database = root / "state" / "workbench.sqlite"
            with self.assertRaises(BusinessError) as caught:
                load_workspace(
                    bundle_dir=directory,
                    db_path=database,
                    reset=True,
                )
            self.assertEqual(caught.exception.code, "E_BAD_NODE")
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
