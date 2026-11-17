from __future__ import annotations

import json
import unittest

from lbhealth.consistency import validate_round
from lbhealth.engine import HealthEngine
from lbhealth.models import HealthState

from .helpers import PlanProber, make_config


class StateMachineTests(unittest.TestCase):
    def test_thresholds_slow_start_and_round_end_weights(self) -> None:
        config = make_config()
        engine = HealthEngine(config)
        plan = {
            1: {"alpha": True, "bravo": False},
            2: {"alpha": True, "bravo": False},
            3: {"alpha": True, "bravo": True},
            4: {"alpha": True, "bravo": True},
            5: {"alpha": True, "bravo": True},
           6: {"alpha": True, "bravo": True},
            7: {"alpha": True, "bravo": True},
        }
        prober = PlanProber(config, plan)
        results = [engine.complete_round(round_no, prober.probe_all(round_no)) for round_no in range(1, 8)]

        r2 = results[1]
        self.assertEqual(r2.state_after.member_states["alpha"].state, HealthState.HEALTHY)
        self.assertEqual(r2.state_after.member_states["bravo"].state, HealthState.UNHEALTHY)
        self.assertEqual(r2.effective_weights, {"alpha": 15, "bravo": 0})
        self.assertEqual(r2.available_members, ("alpha",))
        self.assertEqual(
            [(item.member_id, item.from_state.value, item.to_state.value) for item in r2.transitions],
            [
                ("alpha", "unknown", "healthy"),
                ("bravo", "unknown", "unhealthy"),
            ],
        )

        self.assertEqual(results[4].effective_weights, {"alpha": 60, "bravo": 20})
        r7 = results[6]
        self.assertEqual(r7.effective_weights, {"alpha": 60, "bravo": 40})
        self.assertEqual(r7.effective_weight_sum, 100)
        self.assertEqual(validate_round(config, r7)["status"], "ok")
        self.assertEqual(r7.state_after.member_states["alpha"].success_count, 2)

    def test_failure_interrupts_slow_start_and_recovery_restarts_it(self) -> None:
        config = make_config()
        engine = HealthEngine(config)
        plan = {
            1: {"alpha": True, "bravo": True},
            2: {"alpha": True, "bravo": True},
            3: {"alpha": True, "bravo": True},
            4: {"alpha": False, "bravo": True},
            5: {"alpha": False, "bravo": True},
            6: {"alpha": True, "bravo": True},
            7: {"alpha": True, "bravo": True},
        }
        prober = PlanProber(config, plan)
        results = [engine.complete_round(r, prober.probe_all(r)) for r in range(1, 8)]
        self.assertEqual(results[2].state_after.member_states["alpha"].slow_start_index, 1)
        self.assertEqual(results[3].state_after.member_states["alpha"].slow_start_index, 0)
        self.assertEqual(results[4].state_after.member_states["alpha"].state, HealthState.UNHEALTHY)
        self.assertEqual(results[5].state_after.member_states["alpha"].state, HealthState.UNHEALTHY)
        self.assertEqual(results[6].state_after.member_states["alpha"].state, HealthState.HEALTHY)
        self.assertEqual(results[6].state_after.member_states["alpha"].slow_start_index, 0)
        self.assertEqual(results[6].effective_weights["alpha"], 15)
        transition = results[6].transitions[0]
        self.assertEqual((transition.from_state, transition.to_state), (HealthState.UNHEALTHY, HealthState.HEALTHY))
        self.assertIn("slow start restarted", transition.reason)

    def test_draining_completes_round_then_leaves_available_set(self) -> None:
        config = make_config()
        engine = HealthEngine(config)
        prober = PlanProber(
            config,
            {
                1: {"alpha": True, "bravo": True},
                2: {"alpha": True, "bravo": True},
                3: {"alpha": True, "bravo": True},
            },
        )
        engine.complete_round(1, prober.probe_all(1))
        engine.complete_round(2, prober.probe_all(2))
        engine.request_drain("bravo", "test drain")
        pre = engine.prepare_round(3)
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0].to_state, HealthState.DRAINING)
        result = engine.complete_round(3, prober.probe_all(3), pre)
        self.assertEqual(result.state_after.member_states["bravo"].state, HealthState.DRAINING)
        self.assertNotIn("bravo", result.available_members)
        self.assertEqual(result.effective_weights["bravo"], 0)
        self.assertIn("bravo", [probe.member_id for probe in result.probes])

        engine.request_undrain("bravo", "test resume")
        pre4 = engine.prepare_round(4)
        self.assertEqual(pre4[0].from_state, HealthState.DRAINING)
        self.assertEqual(pre4[0].to_state, HealthState.UNKNOWN)

    def test_history_is_not_retroactively_modified(self) -> None:
        config = make_config()
        engine = HealthEngine(config)
        prober = PlanProber(
            config,
            {
                1: {"alpha": True, "bravo": True},
                2: {"alpha": True, "bravo": True},
                3: {"alpha": False, "bravo": False},
            },
        )
        r1 = engine.complete_round(1, prober.probe_all(1))
        snapshot = json.dumps(r1.to_dict(), sort_keys=True)
        engine.complete_round(2, prober.probe_all(2))
        engine.complete_round(3, prober.probe_all(3))
        self.assertEqual(json.dumps(r1.to_dict(), sort_keys=True), snapshot)

    def test_weight_formula_consistency_every_round(self) -> None:
        config = make_config()
        engine = HealthEngine(config)
        prober = PlanProber(
            config,
            {round_no: {"alpha": True, "bravo": round_no % 2 == 0} for round_no in range(1, 7)},
        )
        for round_no in range(1, 7):
            result = engine.complete_round(round_no, prober.probe_all(round_no))
            check = validate_round(config, result)
            self.assertEqual(check["status"], "ok", check)
            self.assertEqual(check["effective_weight_sum"], sum(result.effective_weights.values()))


if __name__ == "__main__":
    unittest.main()
