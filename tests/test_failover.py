from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from failover_console.codes import BusinessCode
from failover_console.engine import FailoverEngine

from .helpers import SuccessTransport, instance, load_scene, scene_data, service


def three_instance_data(*, mode: str = "auto", min_healthy: int = 1) -> dict[str, Any]:
    return scene_data(
        [
            service(
                "payments",
                [
                    instance("payments-a", "primary", 49701),
                    instance("payments-b", "standby", 49702),
                    instance("payments-c", "standby", 49703),
                ],
                mode=mode,
                cooldown_ms=60_000,
                min_healthy_replicas=min_healthy,
            )
        ]
    )


class FailoverTests(unittest.TestCase):
    def test_target_selection_is_deterministic_and_versions_are_atomic(self) -> None:
        scene = load_scene(three_instance_data())
        with TemporaryDirectory() as td, FailoverEngine(
            scene, Path(td) / "state.sqlite", transport=SuccessTransport()
        ) as engine:
            engine.run_cycle(now_epoch_ms_override=1_000_000)
            result = engine.run_cycle(
                runtime_faults={"payments-a": "unreachable"},
                now_epoch_ms_override=1_000_100,
            )
            switched = [item for item in result.switch_decisions if item.switched]
            self.assertEqual(len(switched), 1)
            self.assertEqual(switched[0].source_instance, "payments-a")
            self.assertEqual(switched[0].target_instance, "payments-b")
            source = engine.runtime["payments-a"]
            target = engine.runtime["payments-b"]
            self.assertEqual((source.role, source.version, source.isolated), ("standby", 2, True))
            self.assertEqual((target.role, target.version), ("primary", 2))
            self.assertEqual(sum(1 for item in engine.runtime.values() if item.role == "primary"), 1)

    def test_cooldown_suppresses_repeated_switch_and_isolation_stays(self) -> None:
        scene = load_scene(three_instance_data())
        with TemporaryDirectory() as td, FailoverEngine(
            scene, Path(td) / "state.sqlite", transport=SuccessTransport()
        ) as engine:
            engine.run_cycle(now_epoch_ms_override=2_000_000)
            engine.run_cycle(
                runtime_faults={"payments-a": "unreachable"},
                now_epoch_ms_override=2_000_100,
            )
            result = engine.run_cycle(
                runtime_faults={"payments-b": "unreachable"},
                now_epoch_ms_override=2_000_200,
            )
            self.assertIn(BusinessCode.E_COOLDOWN.value, result.codes)
            cooldown = next(
                item for item in result.switch_decisions if item.code == BusinessCode.E_COOLDOWN.value
            )
            self.assertEqual(cooldown.target_instance, "payments-c")
            self.assertEqual(engine.runtime["payments-b"].role, "primary")
            self.assertTrue(engine.runtime["payments-a"].isolated)
            replay = engine.replay_latest()
            self.assertTrue(replay.ok, [item.as_dict() for item in replay.differences])

    def test_manual_policy_registers_pending_then_can_be_approved(self) -> None:
        scene = load_scene(three_instance_data(mode="manual"))
        with TemporaryDirectory() as td, FailoverEngine(
            scene, Path(td) / "state.sqlite", transport=SuccessTransport()
        ) as engine:
            result = engine.run_cycle(
                runtime_faults={"payments-a": "unreachable"},
                now_epoch_ms_override=3_000_000,
            )
            self.assertIn(BusinessCode.E_MANUAL_PENDING.value, result.codes)
            pending = engine.repo.list_pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["target_instance"], "payments-b")
            self.assertEqual(engine.runtime["payments-a"].role, "primary")
            approved = engine.approve_manual(
                pending[0]["request_id"], now_epoch_ms_override=3_000_100
            )
            self.assertEqual(approved.switch_decisions[0].code, BusinessCode.SWITCHED.value)
            self.assertEqual(engine.runtime["payments-b"].role, "primary")
            self.assertTrue(engine.runtime["payments-a"].isolated)
            self.assertEqual(engine.repo.list_pending()[0]["status"], "approved")

    def test_concurrent_switch_requests_have_one_success_and_e_busy(self) -> None:
        scene = load_scene(three_instance_data(mode="manual"))
        with TemporaryDirectory() as td, FailoverEngine(
            scene, Path(td) / "state.sqlite", transport=SuccessTransport()
        ) as engine:
            engine.run_cycle(
                runtime_faults={"payments-a": "unreachable"},
                now_epoch_ms_override=4_000_000,
            )
            source = "payments-a"
            target = engine.runtime["payments-b"]
            started = threading.Event()
            release = threading.Event()

            def hook(service_id: str) -> None:
                started.set()
                release.wait(timeout=5)

            engine.switch_hook = hook
            results: list[Any] = []
            errors: list[BaseException] = []

            def invoke() -> None:
                try:
                    decision = engine._perform_switch(
                        service_id="payments",
                        source_instance=source,
                        target=target,
                        trigger_reason="primary_unhealthy",
                        run_id="run-concurrent",
                        round_no=1,
                        runtime_faults={},
                        now_epoch_ms=4_000_100,
                        transitions=[],
                        business_events=[],
                        manual_approved=True,
                    )
                    results.append(decision)
                except BaseException as exc:  # captured for assertion in main thread
                    errors.append(exc)

            first = threading.Thread(target=invoke)
            second = threading.Thread(target=invoke)
            first.start()
            self.assertTrue(started.wait(timeout=5))
            second.start()
            second.join(timeout=5)
            release.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            codes = sorted(item.code for item in results)
            self.assertEqual(codes, [BusinessCode.E_BUSY.value, BusinessCode.SWITCHED.value])

    def test_post_switch_consistency_reports_e_isolate_deviations(self) -> None:
        data = scene_data(
            [
                service(
                    "orders",
                    [
                        instance("orders-a", "primary", 49801),
                        instance("orders-b", "standby", 49802),
                    ],
                    min_healthy_replicas=2,
                )
            ]
        )
        scene = load_scene(data)
        with TemporaryDirectory() as td, FailoverEngine(
            scene, Path(td) / "state.sqlite", transport=SuccessTransport()
        ) as engine:
            engine.run_cycle(now_epoch_ms_override=5_000_000)
            result = engine.run_cycle(
                runtime_faults={"orders-a": "unreachable"},
                now_epoch_ms_override=5_000_100,
            )
            self.assertIn(BusinessCode.E_ISOLATE.value, result.codes)
            decision = next(
                item for item in result.switch_decisions if item.switched
            )
            self.assertEqual(decision.code, BusinessCode.E_ISOLATE.value)
            deviations = decision.details["deviations"]
            self.assertTrue(
                any(item["field"] == "healthy_replicas" for item in deviations)
            )
            self.assertTrue(any(item["field"] == "isolated" for item in deviations) is False)
            self.assertTrue(engine.runtime["orders-a"].isolated)


if __name__ == "__main__":
    unittest.main()
