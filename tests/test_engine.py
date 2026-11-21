from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from drillbench.engine import DrillEngine
from drillbench.errors import BusinessError
from drillbench.models import StepObservation
from drillbench.report import build_status
from drillbench.store import StateStore
from drillbench.validation import load_snapshot

from .helpers import (
    make_route,
    make_step,
    make_window,
    minimal_documents,
    write_documents,
)


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "state.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _classification_fixture(self) -> None:
        steps = [make_step(1, timeout=400)]
        windows = [make_window(1, 0, 400)]
        routes = [make_route(1)]
        injections = []

        specs = [
            (2, 500, 300, "bad-status", "E_BAD_STATUS"),
            (3, 900, 300, "malformed", "E_MALFORMED_BODY"),
            (4, 1300, 300, "disconnect", "E_CONNECTION_DROPPED"),
            (5, 1700, 250, "timeout", "E_STEP_TIMEOUT"),
            (6, 2050, 400, "service-reject", "E_SERVICE_REJECTED"),
            (7, 2550, 400, "network-outage", "E_CONN_REFUSED"),
            (8, 3050, 400, "data-corruption", "E_DATA_CORRUPTION"),
            (9, 3550, 400, "clock-rollback", "E_CLOCK_ROLLBACK"),
        ]
        for seq, start, timeout, kind, _ in specs:
            steps.append(make_step(seq, depends_on=[1], timeout=timeout))
            windows.append(make_window(seq, start, timeout))
            if kind == "bad-status":
                routes.append(make_route(seq, status=500))
            elif kind == "malformed":
                routes.append(make_route(seq, mode="malformed"))
            elif kind == "disconnect":
                routes.append(make_route(seq, mode="disconnect"))
            elif kind == "timeout":
                routes.append(make_route(seq, delay=1200))
            else:
                routes.append(make_route(seq))
            injection_type = {
                "service-reject": "service_reject",
                "network-outage": "network_outage",
                "data-corruption": "data_corruption",
                "clock-rollback": "clock_rollback",
            }.get(kind)
            if injection_type:
                injection = {
                    "id": f"inj-{seq}",
                    "step": seq,
                    "at_ms": start,
                    "type": injection_type,
                    "description": kind,
                }
                if injection_type == "clock_rollback":
                    injection["rollback_ms"] = 100
                injections.append(injection)

        documents = minimal_documents(
            steps=steps,
            routes=routes,
            windows=windows,
            injections=injections,
        )
        paths = write_documents(self.root / "sample", documents)
        snapshot = load_snapshot(*paths, plan_name="fixture")
        store = StateStore(self.db)
        store.install_snapshot(snapshot)

    def test_all_error_classes_are_isolated_and_consumed_once(self) -> None:
        self._classification_fixture()
        store = StateStore(self.db)
        engine = DrillEngine(store, run_id="classification")
        result = engine.run(plan_name="fixture")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "COMPLETED")

        status = build_status(store, "classification")
        by_seq = {step["seq"]: step for step in status["steps"]}
        expected = {
            1: "OBSERVED",
            2: "E_BAD_STATUS",
            3: "E_MALFORMED_BODY",
            4: "E_CONNECTION_DROPPED",
            5: "E_STEP_TIMEOUT",
            6: "E_SERVICE_REJECTED",
            7: "E_CONN_REFUSED",
            8: "E_DATA_CORRUPTION",
            9: "E_CLOCK_ROLLBACK",
        }
        for seq, code in expected.items():
            self.assertEqual(by_seq[seq]["outcome_code"], code, seq)
            self.assertEqual(by_seq[seq]["observation"]["step"], seq)
            self.assertGreaterEqual(
                by_seq[seq]["observation"]["attempt_no"], 1
            )
        self.assertEqual(by_seq[5]["stage"], "TIMED_OUT")
        self.assertEqual(
            by_seq[5]["outcome_class"], "TIMED_OUT_AFTER_START"
        )
        self.assertTrue(by_seq[5]["observation"]["detail"]["active_abort"])
        self.assertFalse(
            by_seq[5]["observation"]["detail"][
                "wall_clock_used_for_decision"
            ]
        )
        for seq in (6, 7, 8, 9):
            self.assertIsNotNone(
                by_seq[seq]["observation"]["injected_at_ms"]
            )
        consumed = {
            item["id"]: item for item in status["injections"] if item["consumed"]
        }
        self.assertEqual(
            set(consumed),
            {"inj-6", "inj-7", "inj-8", "inj-9"},
        )
        verification = store.verify_run("classification")
        self.assertTrue(verification["ok"])

    def test_repeat_commit_is_idempotent(self) -> None:
        steps = [make_step(1)]
        routes = [make_route(1)]
        windows = [make_window(1, 0, 400)]
        documents = minimal_documents(
            steps=steps,
            routes=routes,
            windows=windows,
        )
        paths = write_documents(self.root / "idempotent", documents)
        snapshot = load_snapshot(*paths, plan_name="fixture")
        store = StateStore(self.db)
        store.install_snapshot(snapshot)
        engine = DrillEngine(store, run_id="idempotent")
        engine.run(plan_name="fixture", once=True)

        bundle = store.get_run_bundle("idempotent")
        observation_row = bundle["observations"][0]
        step_row = bundle["steps"][0]
        observation = StepObservation(
            step=int(observation_row["step_seq"]),
            attempt_no=int(observation_row["attempt_no"]),
            code=observation_row["code"],
            outcome_class=observation_row["outcome_class"],
            stage=observation_row["stage"],
            event=observation_row["event"],
            response_status=observation_row["response_status"],
            body_sha256=observation_row["body_sha256"],
            injected_at_ms=observation_row["injected_at_ms"],
            injections=tuple(
                json.loads(observation_row["injections_json"])
            ),
            detail=json.loads(observation_row["detail_json"]),
            started_at_ms=step_row["started_at_ms"],
            ended_at_ms=step_row["ended_at_ms"],
        )
        repeated = store.commit_step(
            run_id="idempotent",
            version=1,
            observation=observation,
        )
        self.assertFalse(repeated["changed"])
        self.assertTrue(repeated["idempotent"])

    def test_locked_step_returns_business_error(self) -> None:
        steps = [make_step(1)]
        routes = [make_route(1)]
        windows = [make_window(1, 0, 400)]
        documents = minimal_documents(
            steps=steps,
            routes=routes,
            windows=windows,
        )
        paths = write_documents(self.root / "locked", documents)
        snapshot = load_snapshot(*paths, plan_name="fixture")
        store = StateStore(self.db)
        store.install_snapshot(snapshot)
        engine = DrillEngine(store, run_id="locked")
        with store.step_lock(1, 1):
            with self.assertRaises(BusinessError) as caught:
                engine.run(plan_name="fixture")
        self.assertEqual(caught.exception.code, "E_LOCKED")


if __name__ == "__main__":
    unittest.main()

