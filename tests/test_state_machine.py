from __future__ import annotations

import unittest

from failover_console.models import InstanceRuntime, ProbeAttempt, ProbeResult
from failover_console.state_machine import apply_probe_result, mark_missing_probe_unknown

from .helpers import probe_config


def runtime(
    *,
    state: str = "unknown",
    success_threshold: int = 1,
    failure_threshold: int = 1,
    successes: int = 0,
    failures: int = 0,
) -> InstanceRuntime:
    return InstanceRuntime(
        service_id="svc",
        instance_id="svc-a",
        role="primary",
        port=49601,
        version=1,
        isolated=False,
        health_state=state,
        consecutive_successes=successes,
        consecutive_failures=failures,
        probe=probe_config(
            success_threshold=success_threshold,
            failure_threshold=failure_threshold,
        ),
        requires_reprobe=False,
        updated_at_epoch_ms=0,
    )


def result(conclusion: str) -> ProbeResult:
    attempt = ProbeAttempt(
        run_id="r",
        round_no=1,
        service_id="svc",
        instance_id="svc-a",
        attempt_no=1,
        conclusion=conclusion,
        detail="x",
        started_mono_ms=1,
        ended_mono_ms=2,
        created_epoch_ms=3,
    )
    return ProbeResult("svc", "svc-a", (attempt,), conclusion)


class StateMachineTests(unittest.TestCase):
    def test_success_threshold_moves_unknown_to_healthy(self) -> None:
        item = runtime(success_threshold=2)
        self.assertIsNone(apply_probe_result(item, result("success")))
        self.assertEqual(item.health_state, "unknown")
        change = apply_probe_result(item, result("success"))
        self.assertIsNotNone(change)
        assert change is not None
        self.assertEqual((change.from_state, change.to_state), ("unknown", "healthy"))
        self.assertEqual(change.reason, "success_threshold")

    def test_failure_threshold_moves_healthy_to_unhealthy(self) -> None:
        item = runtime(state="healthy", failure_threshold=2)
        self.assertIsNone(apply_probe_result(item, result("timeout")))
        self.assertEqual(item.health_state, "healthy")
        change = apply_probe_result(item, result("timeout"))
        self.assertIsNotNone(change)
        assert change is not None
        self.assertEqual((change.from_state, change.to_state), ("healthy", "unhealthy"))
        self.assertEqual(change.reason, "failure_threshold")

    def test_missing_probe_never_becomes_healthy(self) -> None:
        item = runtime(state="healthy")
        change = mark_missing_probe_unknown(item)
        self.assertIsNotNone(change)
        self.assertEqual(item.health_state, "unknown")
        self.assertTrue(item.requires_reprobe)
        self.assertIsNone(mark_missing_probe_unknown(runtime()))


if __name__ == "__main__":
    unittest.main()
