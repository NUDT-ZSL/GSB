from __future__ import annotations

import unittest

from flight_compile_cache.compiler import artifact_identity, compile_scope
from flight_compile_cache.generator import generate_project
from flight_compile_cache.graph import plan_request_graph
from flight_compile_cache.keys import make_compile_key


class CompileKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = generate_project()
        cls.delay_requests = [
            request
            for request in cls.project["requests"]
            if request["model_id"] == "delay_predict"
        ]

    def _plan(self, shape, version="v1"):
        request = {
            "id": "unit",
            "time": 0,
            "model_id": "delay_predict",
            "model_version": version,
            "input_shapes": shape,
        }
        current = {"delay_predict": version}
        return plan_request_graph(self.project, request, current_versions=current)

    def test_same_version_shape_key_is_stable(self) -> None:
        request = self.delay_requests[0]
        one = make_compile_key("delay_predict", "v1", request["input_shapes"])
        two = make_compile_key(
            "delay_predict",
            "v1",
            dict(reversed(list(request["input_shapes"].items()))),
        )
        self.assertEqual(one, two)
        self.assertRegex(one, r"^ck_[0-9a-f]{24}$")

    def test_version_and_shape_change_key(self) -> None:
        shape = self.delay_requests[0]["input_shapes"]
        v1 = make_compile_key("delay_predict", "v1", shape)
        v2 = make_compile_key("delay_predict", "v2", shape)
        changed = {name: list(value) for name, value in shape.items()}
        changed["flight_features"][0] += 1
        changed_key = make_compile_key("delay_predict", "v1", changed)
        self.assertNotEqual(v1, v2)
        self.assertNotEqual(v1, changed_key)
        self.assertNotEqual(v2, changed_key)

    def test_shape_independent_artifacts_reuse_and_specialized_artifacts_change(self) -> None:
        shape3 = {
            "flight_features": [3, 12],
            "weather": [3, 4],
            "slot_mask": [3, 8],
        }
        shape4 = {
            "flight_features": [4, 12],
            "weather": [4, 4],
            "slot_mask": [4, 8],
        }
        plan3 = self._plan(shape3)
        plan4 = self._plan(shape4)
        for node_id in plan3["topo_nodes"]:
            node = plan3["nodes_by_id"][node_id]
            _, art3, _ = artifact_identity("delay_predict", node, plan3)
            _, art4, _ = artifact_identity("delay_predict", node, plan4)
            if compile_scope(node) == "shape_independent":
                self.assertEqual(art3, art4, node_id)
            else:
                self.assertNotEqual(art3, art4, node_id)

    def test_unaffected_artifacts_stable_across_model_update(self) -> None:
        shape = {
            "flight_features": [3, 12],
            "weather": [3, 4],
            "slot_mask": [3, 8],
        }
        plan_v1 = self._plan(shape, "v1")
        plan_v2 = self._plan(shape, "v2")
        for node_id in plan_v1["topo_nodes"]:
            node = plan_v1["nodes_by_id"][node_id]
            _, art1, _ = artifact_identity("delay_predict", node, plan_v1)
            _, art2, _ = artifact_identity("delay_predict", node, plan_v2)
            if node_id == "delay_mean":
                self.assertNotEqual(art1, art2)
            else:
                self.assertEqual(art1, art2, node_id)


if __name__ == "__main__":
    unittest.main()
