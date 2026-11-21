from __future__ import annotations

import copy
import json
import socket
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from capacity_engine.clock import build_schedule
from capacity_engine.engine import Engine
from capacity_engine.errors import BizError
from capacity_engine.scene import load_scene, parse_scene
from capacity_engine.store import Store
from capacity_engine.util import canonical_json, sha256_json

from tests.helpers import mock_server, unused_loopback_port, write_json


def _error_scene(mock_port: int, refused_port: int) -> dict:
    return {
        "scene_id": "error-isolation-v1",
        "services": [
            {
                "name": "mock-service",
                "address": f"http://127.0.0.1:{mock_port}",
                "success_status_codes": [200],
                "tenants": {
                    "malformed": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    },
                    "interrupted": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    },
                    "status-mismatch": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    },
                    "budget": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    },
                    "healthy": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    },
                },
            },
            {
                "name": "refused-service",
                "address": f"http://127.0.0.1:{refused_port}",
                "success_status_codes": [200],
                "tenants": {
                    "refused": {
                        "success_rate_min": 0.9,
                        "p95_latency_max_ms": 100,
                        "concurrency_limit": 2,
                    }
                },
            },
        ],
        "batches": [
            {
                "batch_id": f"batch-{tenant}",
                "tenant": tenant,
                "concurrency_gradients": [1],
                "duration_logical_ms": 5,
                "request_pace_ms": 5,
                "allow_early_termination": True,
            }
            for tenant in ["malformed", "interrupted", "status-mismatch", "budget", "healthy", "refused"]
        ],
        "clock": {"start_ms": 0, "window_ms": 5, "step_table": [5]},
        "precision": {
            "latency_scale": 1000,
            "percent_scale": 10000,
            "compare_tolerance": {"success_rate": 0.05, "p95_latency_ms": 2},
            "degraded_margin": {"success_rate": 0.005, "p95_latency_ms": 0.1},
            "error_budget": {"success_rate": 0.001, "p95_latency_ms": 2},
            "request_logical_budget_ms": 100,
        },
        "budget": {"global_logical_time_ms": 1000},
    }


def _error_mock_config() -> dict:
    return {
        "tenants": {
            "malformed": {"default": {"status": 200, "latency_us": 1000, "malformed": True}},
            "interrupted": {"default": {"status": 503, "latency_us": 1000}},
            "status-mismatch": {"default": {"status": 418, "latency_us": 1000}},
            "budget": {"default": {"status": 200, "latency_us": 999999}},
            "healthy": {"default": {"status": 200, "latency_us": 1000}},
        }
    }


class RequestErrorTests(unittest.TestCase):
    def test_error_codes_are_distinct_and_isolated(self) -> None:
        with mock_server_from(_error_mock_config()) as port, tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            scene_path = write_json(base / "errors.json", _error_scene(port, unused_loopback_port()))
            scene = load_scene(scene_path)
            result = Engine(scene, base / "errors.sqlite").run()
            codes = {item["code"] for item in result["report"]["request_errors"]}
            self.assertEqual(
                codes,
                {
                    "E_CONNECTION_REFUSED",
                    "E_REQUEST_BUDGET_EXCEEDED",
                    "E_STATUS_CODE_MISMATCH",
                    "E_MALFORMED_RESPONSE",
                    "E_TARGET_INTERRUPTED",
                },
            )
            healthy_windows = [
                item
                for item in result["report"]["windows"]
                if item["tenant"] == "healthy"
            ]
            self.assertEqual(len(healthy_windows), 1)
            self.assertEqual(healthy_windows[0]["success_count"], 1)
            self.assertTrue(result["report"]["analysis_complete"])

    def test_window_summary_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            document = _error_scene(unused_loopback_port(), unused_loopback_port())
            scene = parse_scene(document)
            schedule = build_schedule(scene)
            store = Store(base / "mismatch.sqlite")
            store.initialize(scene, schedule)
            unit = schedule.units[0]
            window = unit.windows[0]
            ledger_entries = []
            summary = {
                "window_key": window.window_key,
                "batch_id": unit.batch_id,
                "service": unit.service,
                "tenant": unit.tenant,
                "gradient_index": unit.gradient_index,
                "window_index": window.window_index,
                "start_ms": window.start_ms,
                "end_ms": window.end_ms,
                "request_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "success_rate_scaled": 0,
                "p95_latency_scaled": None,
                "payload_hash": sha256_json({"v": 1}),
            }
            store.commit_window(unit.unit_key, summary, {"v": 1}, ledger_entries)
            summary2 = copy.deepcopy(summary)
            summary2["payload_hash"] = sha256_json({"v": 2})
            with self.assertRaises(BizError) as caught:
                store.commit_window(unit.unit_key, summary2, {"v": 2}, ledger_entries)
            self.assertEqual(caught.exception.code, "E_WINDOW_SUMMARY_MISMATCH")


class _mock_server_from:
    def __init__(self, config: dict):
        self.config = config
        self._tempdir = tempfile.TemporaryDirectory()
        self._path = Path(self._tempdir.name) / "mock.json"
        self._path.write_text(json.dumps(config), encoding="utf-8")
        self._context = mock_server(self._path)

    def __enter__(self):
        return self._context.__enter__()

    def __exit__(self, exc_type, exc, tb):
        result = self._context.__exit__(exc_type, exc, tb)
        self._tempdir.cleanup()
        return result


def mock_server_from(config: dict):
    return _mock_server_from(config)


if __name__ == "__main__":
    unittest.main()

