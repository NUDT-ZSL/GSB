from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from drillbench.errors import BusinessError
from drillbench.validation import load_snapshot

from .helpers import SAMPLES, cli, load_json, write_json


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plans = load_json(SAMPLES / "plans.json")
        self.services = load_json(SAMPLES / "services.json")
        self.clock = load_json(SAMPLES / "clock.json")
        self.injections = load_json(SAMPLES / "injections.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _paths(self, name: str) -> tuple[Path, Path, Path, Path]:
        base = self.root / name
        return (
            base / "plans.json",
            base / "services.json",
            base / "clock.json",
            base / "injections.json",
        )

    def _write(
        self,
        name: str,
        plans: dict,
        services: dict,
        clock: dict,
        injections: dict,
    ) -> tuple[Path, Path, Path, Path]:
        paths = self._paths(name)
        for path, value in zip(
            paths, (plans, services, clock, injections)
        ):
            write_json(path, value)
        return paths

    def _load(
        self,
        name: str,
        plans: dict,
        services: dict,
        clock: dict,
        injections: dict,
    ):
        paths = self._write(name, plans, services, clock, injections)
        return load_snapshot(*paths, plan_name="payment-failover")

    def test_duplicate_step_sequence_is_rejected(self) -> None:
        plans = copy.deepcopy(self.plans)
        plans["plans"][0]["steps"][1]["seq"] = 1
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "duplicate",
                plans,
                self.services,
                self.clock,
                self.injections,
            )
        self.assertEqual(caught.exception.code, "E_BAD_PLAN")

    def test_cycle_is_rejected(self) -> None:
        plans = copy.deepcopy(self.plans)
        plans["plans"][0]["steps"][0]["depends_on"] = [5]
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "cycle",
                plans,
                self.services,
                self.clock,
                self.injections,
            )
        self.assertEqual(caught.exception.code, "E_CYCLE")

    def test_injection_outside_window_is_rejected(self) -> None:
        injections = copy.deepcopy(self.injections)
        injections["plans"][0]["injections"][0]["at_ms"] = 901
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "bad-inject",
                self.plans,
                self.services,
                self.clock,
                injections,
            )
        self.assertEqual(caught.exception.code, "E_BAD_INJECT")

    def test_bad_expected_event_is_rejected(self) -> None:
        plans = copy.deepcopy(self.plans)
        plans["plans"][0]["steps"][0]["expected_event"] = "MAGIC_OK"
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "bad-event",
                plans,
                self.services,
                self.clock,
                self.injections,
            )
        self.assertEqual(caught.exception.code, "E_BAD_PLAN")

    def test_clock_window_must_match_timeout(self) -> None:
        clock = copy.deepcopy(self.clock)
        clock["plans"][0]["step_windows"][0]["end_ms"] = 499
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "bad-clock",
                self.plans,
                self.services,
                clock,
                self.injections,
            )
        self.assertEqual(caught.exception.code, "E_BAD_CLOCK")

    def test_missing_route_is_rejected(self) -> None:
        services = copy.deepcopy(self.services)
        services["plans"][0]["routes"].pop()
        with self.assertRaises(BusinessError) as caught:
            self._load(
                "bad-service",
                self.plans,
                services,
                self.clock,
                self.injections,
            )
        self.assertEqual(caught.exception.code, "E_BAD_SERVICE")

    def test_invalid_input_via_cli_does_not_create_database(self) -> None:
        plans = copy.deepcopy(self.plans)
        plans["plans"][0]["steps"].append(
            copy.deepcopy(plans["plans"][0]["steps"][0])
        )
        paths = self._write(
            "cli-invalid",
            plans,
            self.services,
            self.clock,
            self.injections,
        )
        db_path = self.root / "must-not-exist" / "state.db"
        code, payload, _, _ = cli(
            "load",
            "--db",
            str(db_path),
            "--plans",
            str(paths[0]),
            "--services",
            str(paths[1]),
            "--clock",
            str(paths[2]),
            "--injections",
            str(paths[3]),
            "--plan",
            "payment-failover",
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "E_BAD_PLAN")
        self.assertFalse(db_path.exists())


if __name__ == "__main__":
    unittest.main()

