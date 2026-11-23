import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scaler.merger import PolicyMerger, convert_v1_relative
from scaler.models import Service, V1Policy, Gear, V2Shard, V2Gear


def service(name, upstream=()):
    return Service(name, 2, 1, 1, tuple(upstream), "fd", 1)


class MergeTests(unittest.TestCase):
    def test_v1_relative_conversion_keeps_scale_and_rounding(self):
        absolute, rounding, scaled = convert_v1_relative(30, 5000)
        self.assertEqual(absolute, 35000)
        self.assertEqual(scaled, 30000)
        self.assertEqual(rounding, "exact")

    def test_v2_wins_on_higher_logical_clock(self):
        services = [service("svc")]
        v1 = [V1Policy("svc", (Gear(0, "0.5"), Gear(10, "1")))]
        shard = V2Shard(
            "s1", 2, "h", 9000, ("svc",),
            (("svc", 2, V2Gear(9000, "1")),), (),
        )
        merger = PolicyMerger(services, v1, [shard], injected_clock_ms=5000)
        unified = merger.merge()
        self.assertEqual(unified["svc"].source_version, "v2")
        self.assertIn("v2 clock", unified["svc"].decision_basis)
        self.assertEqual(unified["svc"].gears[0].raw_time, "9000ms")

    def test_v1_wins_when_injected_clock_newer(self):
        services = [service("svc")]
        v1 = [V1Policy("svc", (Gear(0, "1"),))]
        shard = V2Shard(
            "s1", 2, "h", 1000, ("svc",),
            (("svc", 2, V2Gear(1000, "1")),), (),
        )
        merger = PolicyMerger(services, v1, [shard], injected_clock_ms=8000)
        unified = merger.merge()
        self.assertEqual(unified["svc"].source_version, "v1")
        gear = unified["svc"].gears[0]
        self.assertEqual(gear.absolute_ms, 8000)
        self.assertEqual(gear.raw_time, "0s")
        self.assertEqual(gear.conversion_scale, 1000)

    def test_equal_clock_prefers_v2(self):
        services = [service("svc")]
        v1 = [V1Policy("svc", (Gear(0, "1"),))]
        shard = V2Shard(
            "s1", 2, "h", 5000, ("svc",),
            (("svc", 2, V2Gear(5000, "1")),), (),
        )
        merger = PolicyMerger(services, v1, [shard], injected_clock_ms=5000)
        unified = merger.merge()
        self.assertEqual(unified["svc"].source_version, "v2")
        self.assertIn("source priority", unified["svc"].decision_basis)

    def test_tombstone_flag_propagates(self):
        services = [service("svc")]
        shard = V2Shard(
            "s1", 3, "h", 5000, ("svc",),
            (("svc", 3, V2Gear(5000, "1")),),
            ({"service": "svc", "generation": 3},),
        )
        merger = PolicyMerger(services, [], [shard], 5000)
        self.assertTrue(merger.merge()["svc"].tombstoned)


if __name__ == "__main__":
    unittest.main()
