"""配置校验单元测试：覆盖任务第 1 条的全部拒绝路径。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
import tempfile

from sqli_audit.config import ConfigError, load_config

_VALID = {
    "target_name": "t",
    "base_url": "http://127.0.0.1:8031",
    "endpoint_path": "/orders",
    "method": "GET",
    "baseline": {"order_no": "ORD-1001", "customer_id": "C001"},
    "params": [
        {"name": "order_no", "location": "query", "type": "string", "label": "订单号"},
        {"name": "customer_id", "location": "query", "type": "string", "label": "客户号"},
    ],
    "data_path": ["data", "orders"],
    "order_id_field": "order_id",
    "timeout_seconds": 5,
}


class ConfigValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def _write(self, payload: object) -> Path:
        p = self.tmp / "cfg.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_valid_config_loads_and_sorts_params(self) -> None:
        cfg = load_config(self._write(_VALID))
        self.assertEqual([p.name for p in cfg.params], ["customer_id", "order_no"])
        self.assertEqual(cfg.url, "http://127.0.0.1:8031/orders")

    def test_missing_fields_listed_by_name(self) -> None:
        bad = dict(_VALID)
        del bad["target_name"]
        del bad["params"]
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(bad))
        joined = " | ".join(ctx.exception.problems)
        self.assertIn("target_name", joined)
        self.assertIn("params", joined)

    def test_wrong_type_points_to_field(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        bad["timeout_seconds"] = "five"
        bad["params"][0]["type"] = 999
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(bad))
        joined = " | ".join(ctx.exception.problems)
        self.assertIn("timeout_seconds", joined)
        self.assertTrue(any("params[0].type" in x for x in ctx.exception.problems))

    def test_non_loopback_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        bad["base_url"] = "http://8.8.8.8:80"
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(bad))
        self.assertTrue(any("回环" in x for x in ctx.exception.problems))

    def test_https_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        bad["base_url"] = "https://127.0.0.1:8031"
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))

    def test_missing_nested_param_field_located(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        del bad["params"][1]["label"]
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(bad))
        self.assertTrue(any("params[1].label" in x for x in ctx.exception.problems))

    def test_baseline_key_without_param_definition(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        bad["baseline"] = {"ghost": "x"}
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(bad))
        self.assertTrue(any("baseline.ghost" in x for x in ctx.exception.problems))

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.tmp / "nope.json")

    def test_get_with_body_param_rejected(self) -> None:
        bad = json.loads(json.dumps(_VALID))
        bad["params"][0]["location"] = "body"
        with self.assertRaises(ConfigError):
            load_config(self._write(bad))


if __name__ == "__main__":
    unittest.main()
