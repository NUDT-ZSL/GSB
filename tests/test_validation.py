from __future__ import annotations

import copy
import unittest

from gateway.canonical import loads_strict
from gateway.errors import GatewayError
from gateway.store import Store
from gateway.validation import normalize_inputs
from tests.helpers import (
    TempDirTestCaseMixin,
    config_doc,
    load_cli,
    make_inputs,
    request,
    request_doc,
    service_doc,
    write_json,
    run_cli,
)


class ValidationTests(TempDirTestCaseMixin, unittest.TestCase):
    def assert_code(self, code: str, function, *args, **kwargs) -> GatewayError:
        with self.assertRaises(GatewayError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_duplicate_service(self) -> None:
        services = service_doc()
        services["services"].append(copy.deepcopy(services["services"][0]))
        self.assert_code(
            "E_DUPLICATE_SERVICE",
            normalize_inputs,
            services,
            request_doc([request(1)]),
            config_doc(),
        )

    def test_duplicate_replica(self) -> None:
        services = service_doc(
            replicas=[
                {"replica_id": "r1", "slots": 1, "compute_units": 1},
                {"replica_id": "r1", "slots": 1, "compute_units": 1},
            ]
        )
        self.assert_code(
            "E_DUPLICATE_REPLICA",
            normalize_inputs,
            services,
            request_doc([request(1)]),
            config_doc(),
        )

    def test_duplicate_request(self) -> None:
        self.assert_code(
            "E_DUPLICATE_REQUEST",
            normalize_inputs,
            service_doc(),
            request_doc([request(1), request(1)]),
            config_doc(),
        )

    def test_integer_and_number_fields(self) -> None:
        cases = [
            (
                "E_INVALID_SLOTS",
                service_doc(
                    replicas=[{"replica_id": "r1", "slots": 0, "compute_units": 1}]
                ),
                request_doc([request(1)]),
                config_doc(),
            ),
            (
                "E_INVALID_COMPUTE",
                service_doc(
                    replicas=[{"replica_id": "r1", "slots": 1, "compute_units": 0}]
                ),
                request_doc([request(1)]),
                config_doc(),
            ),
            (
                "E_INVALID_BATCH_WINDOW",
                service_doc(batch_window_ms=0),
                request_doc([request(1)]),
                config_doc(),
            ),
            (
                "E_INVALID_ARRIVAL_CLOCK",
                service_doc(),
                request_doc([request(1, arrival_ms=-1)]),
                config_doc(),
            ),
            (
                "E_INVALID_TOKEN_COUNT",
                service_doc(),
                request_doc([request(1, input_tokens=0)]),
                config_doc(),
            ),
            (
                "E_INVALID_REQUEST_ID",
                service_doc(),
                request_doc([request(0)]),
                config_doc(),
            ),
        ]
        for code, services, requests, config in cases:
            with self.subTest(code=code):
                self.assert_code(
                    code, normalize_inputs, services, requests, config
                )

    def test_priority_and_service_errors(self) -> None:
        error = self.assert_code(
            "E_REQUEST_PRIORITY",
            normalize_inputs,
            service_doc(),
            request_doc([request(1, priority=99)]),
            config_doc(),
        )
        self.assertEqual(error.details["request_id"], 1)
        error = self.assert_code(
            "E_SERVICE_NOT_FOUND",
            normalize_inputs,
            service_doc(),
            request_doc([request(1, service="missing")]),
            config_doc(),
        )
        self.assertEqual(error.details["service"], "missing")

    def test_config_errors(self) -> None:
        service = service_doc()
        requests = request_doc([request(1)])
        cases = [
            ("E_INVALID_TOKEN_CAPACITY", {"global": {"capacity": 0}}),
            (
                "E_INVALID_TOKEN_RATE",
                {"service": {"refill_rate_per_second": 0}},
            ),
            ("E_INVALID_BACKOFF", {"backoff": {"cap_ms": 1, "base_ms": 2}}),
            ("E_INVALID_FIXED_POINT", {"fixed_point": {"scale": 0}}),
            ("E_INVALID_FIXED_POINT", {"fixed_point": {"tolerance_ms": 0}}),
            ("E_INVALID_ROUND_BUDGET", {"round_budget_ms": 0}),
            ("E_INVALID_DISPATCH_TIMEOUT", {"dispatch_timeout_ms": 0}),
        ]
        for code, override in cases:
            config = config_doc()
            for key, value in override.items():
                if isinstance(value, dict):
                    config[key].update(value)
                else:
                    config[key] = value
            with self.subTest(code=code, override=override):
                self.assert_code(
                    code, normalize_inputs, service, requests, config
                )

    def test_duplicate_json_key(self) -> None:
        error = self.assert_code(
            "E_JSON_DUPLICATE_KEY",
            loads_strict,
            '{"schema_version":1,"schema_version":1}',
        )
        self.assertIn("schema_version", error.message)

    def test_invalid_load_does_not_create_database(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "never.db"
        result = run_cli(
            "load",
            "--db",
            str(db),
            "--services",
            str(write_json(temp / "services.json", service_doc())),
            "--requests",
            str(
                write_json(
                    temp / "requests.json", request_doc([request(1, priority=99)])
                )
            ),
            "--config",
            str(write_json(temp / "config.json", config_doc())),
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(
            __import__("json").loads(result.stdout)["error"]["code"],
            "E_REQUEST_PRIORITY",
        )
        self.assertFalse(db.exists())

    def test_invalid_clock_step_does_not_create_database(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "never.db"
        result = load_cli(
            db,
            service_doc(),
            request_doc([request(1)]),
            config_doc(),
            clock_steps={
                "schema_version": 1,
                "steps": [
                    {
                        "round_id": "bad-step",
                        "clock_ms": 1,
                        "outcomes": [
                            {
                                "request_id": 1,
                                "generation": 0,
                                "replica_id": "r1",
                                "outcome": "success",
                            }
                        ],
                    }
                ],
            },
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        payload = __import__("json").loads(result.stdout)
        self.assertEqual(payload["error"]["code"], "E_INVALID_COMMAND")
        self.assertFalse(db.exists())

    def test_invalid_load_does_not_touch_existing_ledger(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        result = load_cli(
            db, service_doc(), request_doc([request(1)]), config_doc()
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state, version, _ = Store(db).read_state()
        original = __import__("json").dumps(state, sort_keys=True)

        bad = run_cli(
            "load",
            "--db",
            str(db),
            "--services",
            str(write_json(temp / "services.json", service_doc())),
            "--requests",
            str(
                write_json(
                    temp / "requests.json", request_doc([request(1, priority=99)])
                )
            ),
            "--config",
            str(write_json(temp / "config.json", config_doc())),
        )
        self.assertEqual(bad.returncode, 2)
        state_after, version_after, _ = Store(db).read_state()
        self.assertEqual(version_after, version)
        self.assertEqual(
            __import__("json").dumps(state_after, sort_keys=True), original
        )

    def test_config_version_regression_and_conflict(self) -> None:
        temp = self.make_temp_dir()
        db = temp / "ledger.db"
        v3 = config_doc(config_version=3)
        normalized = make_inputs(
            service_doc(), request_doc([request(1)]), v3
        )
        Store(db).load(normalized)
        v2 = config_doc(config_version=2)
        with self.assertRaises(GatewayError) as caught:
            Store(db).load(
                make_inputs(service_doc(), request_doc([request(1)]), v2)
            )
        self.assertEqual(caught.exception.code, "E_CONFIG_VERSION_REGRESSION")

        conflict = config_doc(config_version=3, capacity=5)
        with self.assertRaises(GatewayError) as caught:
            Store(db).load(
                make_inputs(service_doc(), request_doc([request(1)]), conflict)
            )
        self.assertEqual(caught.exception.code, "E_CONFIG_VERSION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
