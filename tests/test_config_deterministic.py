from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cluster_ledger.config import load_inputs
from cluster_ledger.deterministic import compute_aggregate
from cluster_ledger.errors import E_CONFIG, BusinessError

from tests.helpers import DATA, config_payload, mutate_config, write_config


class ConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _assert_config_error(self, payload, expected_path: str) -> None:
        path = write_config(self.dir / "bad.json", payload)
        with self.assertRaises(BusinessError) as raised:
            load_inputs(path, DATA / "sample_data.json")
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertEqual(raised.exception.field_path, expected_path)

    def test_duplicate_cluster_id_rejected_with_path(self) -> None:
        payload = mutate_config(lambda p: p["clusters"][1].__setitem__("cluster_id", "edge-a"))
        self._assert_config_error(payload, "$.clusters[1].cluster_id")

    def test_duplicate_port_rejected_with_path(self) -> None:
        payload = mutate_config(lambda p: p["clusters"][1].__setitem__("port", p["clusters"][0]["port"]))
        self._assert_config_error(payload, "$.clusters[1].port")

    def test_non_literal_loopback_rejected(self) -> None:
        payload = mutate_config(lambda p: p["clusters"][0].__setitem__("host", "localhost"))
        self._assert_config_error(payload, "$.clusters[0].host")

    def test_non_monotonic_version_history_rejected(self) -> None:
        def mutate(payload):
            payload["clusters"][0]["version_history"] = [2, 1]
            payload["clusters"][0]["config_version"] = 1
        self._assert_config_error(mutate_config(mutate), "$.clusters[0].version_history[1]")

    def test_unknown_task_config_key_rejected(self) -> None:
        payload = mutate_config(lambda p: p["tasks"][0]["config_keys"].append("app.unknown"))
        self._assert_config_error(payload, "$.tasks[0].config_keys[2]")

    def test_undeclared_target_cluster_rejected(self) -> None:
        payload = mutate_config(lambda p: p["tasks"][0]["target_clusters"].append("ghost"))
        self._assert_config_error(payload, "$.tasks[0].target_clusters[3]")

    def test_task_timeout_and_retries_are_required(self) -> None:
        payload = mutate_config(lambda p: p["tasks"][0].pop("timeout_ms"))
        self._assert_config_error(payload, "$.tasks[0].timeout_ms")
        payload = mutate_config(lambda p: p["tasks"][0].pop("retries"))
        self._assert_config_error(payload, "$.tasks[0].retries")

    def test_sample_missing_node_is_configuration_failure(self) -> None:
        with (DATA / "sample_data.json").open("r", encoding="utf-8") as handle:
            sample = json.load(handle)
        del sample["clusters"]["edge-a"]["1"]["app.timeout"]["edge-a-n1"]
        sample_path = self.dir / "sample-missing.json"
        sample_path.write_text(json.dumps(sample), encoding="utf-8")
        config_path = write_config(self.dir / "good.json", config_payload())
        with self.assertRaises(BusinessError) as raised:
            load_inputs(config_path, sample_path)
        self.assertEqual(raised.exception.code, E_CONFIG)
        self.assertIn("edge-a-n1", raised.exception.field_path)

    def test_whole_load_succeeds_only_with_valid_files(self) -> None:
        config_path = write_config(self.dir / "good.json", config_payload())
        declaration, sample = load_inputs(config_path, DATA / "sample_data.json")
        self.assertEqual(len(declaration.clusters), 3)
        self.assertEqual(len(declaration.tasks), 3)
        self.assertIn("edge-a", sample["clusters"])


class DeterministicAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declaration, self.sample = load_inputs(
            DATA / "clusters.json", DATA / "sample_data.json"
        )

    def _aggregate(self, task_id: str, cluster_id: str, version: int | None = None):
        task = self.declaration.task_map()[task_id]
        cluster = self.declaration.cluster_map()[cluster_id]
        return compute_aggregate(
            cluster_id=cluster_id,
            nodes=cluster.nodes,
            config_version=cluster.config_version if version is None else version,
            config_keys=task.config_keys,
            aggregation={"mode": task.aggregation_mode},
            sample_data=self.sample,
        )

    def test_repeat_is_byte_stable_and_version_sensitive(self) -> None:
        first = self._aggregate("runtime-rollout", "edge-a")
        second = self._aggregate("runtime-rollout", "edge-a")
        next_version = self._aggregate("runtime-rollout", "edge-a", version=2)
        self.assertEqual(first.summary, second.summary)
        self.assertEqual(first.components, second.components)
        self.assertNotEqual(first.summary, next_version.summary)

    def test_sum_mode_has_per_key_components(self) -> None:
        result = self._aggregate("capacity-ledger", "edge-a")
        self.assertEqual(result.summary, "sum:9")
        self.assertEqual(result.components, {"app.replicas": "sum:9"})


if __name__ == "__main__":
    unittest.main()
