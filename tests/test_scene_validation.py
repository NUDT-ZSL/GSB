from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from failover_console.codes import BusinessCode
from failover_console.errors import BusinessError
from failover_console.scene import SceneLoader

from .helpers import instance, load_scene, scene_data, service


class SceneValidationTests(unittest.TestCase):
    def test_valid_scene_loading(self) -> None:
        scene = load_scene(
            scene_data(
                [
                    service(
                        "svc",
                        [instance("svc-a", "primary", 49001), instance("svc-b", "standby", 49002)],
                    )
                ]
            )
        )
        self.assertEqual(scene.scene_version, "test-v1")
        self.assertEqual(len(scene.instances()), 2)
        self.assertEqual(len(scene.fingerprint), 64)

    def test_duplicate_global_port_is_rejected_with_line_and_field(self) -> None:
        data = scene_data(
            [
                service(
                    "svc",
                    [instance("svc-a", "primary", 49101), instance("svc-b", "standby", 49101)],
                )
            ]
        )
        with self.assertRaises(BusinessError) as ctx:
            load_scene(data)
        error = ctx.exception
        self.assertEqual(error.code, BusinessCode.E_SCENE.value)
        self.assertTrue(error.issues)
        port_issue = next(issue for issue in error.issues if issue.field.endswith(".port"))
        self.assertGreater(port_issue.line, 1)
        self.assertIn("全局重复", port_issue.message)

    def test_missing_primary_duplicate_service_and_policy_bounds(self) -> None:
        bad_service = service(
            "svc",
            [instance("svc-a", "standby", 49201), instance("svc-b", "standby", 49202)],
            min_healthy_replicas=3,
        )
        data = scene_data([bad_service, json.loads(json.dumps(bad_service))])
        data["services"][0]["instances"][0]["probe"]["timeout_ms"] = 0
        data["services"][0]["policy"]["cooldown_ms"] = 0
        with self.assertRaises(BusinessError) as ctx:
            load_scene(data)
        fields = {issue.field for issue in ctx.exception.issues}
        self.assertTrue(any(field.endswith(".instances") for field in fields))
        self.assertTrue(any(field.endswith(".service_id") for field in fields))
        self.assertTrue(any(field.endswith(".probe.timeout_ms") for field in fields))
        self.assertTrue(any(field.endswith(".policy.cooldown_ms") for field in fields))
        self.assertTrue(any(field.endswith(".policy.min_healthy_replicas") for field in fields))

    def test_malformed_json_reports_decoder_line(self) -> None:
        text = '{\n  "scene_version": "v1",\n  "services": [\n}\n'
        with self.assertRaises(BusinessError) as ctx:
            SceneLoader().loads(text)
        self.assertEqual(ctx.exception.code, BusinessCode.E_SCENE.value)
        self.assertGreaterEqual(ctx.exception.issues[0].line, 4)
        self.assertIn("field $", str(ctx.exception))

    def test_injected_corruption_is_e_scene(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scene.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(BusinessError) as ctx:
                SceneLoader().load(path, inject_corruption=True)
        self.assertEqual(ctx.exception.code, BusinessCode.E_SCENE.value)
        self.assertEqual(ctx.exception.issues[0].line, 1)

    def test_port_override_cannot_create_duplicate_ports(self) -> None:
        scene = load_scene(
            scene_data(
                [
                    service(
                        "svc",
                        [instance("svc-a", "primary", 50001), instance("svc-b", "standby", 50002)],
                    )
                ]
            )
        )
        with self.assertRaises(ValueError):
            scene.with_port_overrides({"svc-b": 50001})


if __name__ == "__main__":
    unittest.main()
