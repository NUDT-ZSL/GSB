from __future__ import annotations

import os
import random
import subprocess
import sys
import unittest

from alarm_domain.clustering import cluster_events, cluster_summary_bytes
from alarm_domain.models import AlarmEvent, Edge, Rule, Topology
from alarm_domain.rootcause import analyze_clusters, derive_root_causes
from alarm_domain.session import AlarmSession

from .helpers import EVENTS, RULES, TOPOLOGY


def _event(event_id: str, service: str, timestamp: int, rule_id: str = "R") -> AlarmEvent:
    return AlarmEvent(
        event_id=event_id,
        rule_id=rule_id,
        service=service,
        timestamp=timestamp,
        value=1.0,
    )


class ClusteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.topology = Topology(
            services=("a", "b", "c", "d"),
            edges=(
                Edge("a", "b"),
                Edge("b", "c"),
            ),
        )
        self.events = (
            _event("e-a-1", "a", 100),
            _event("e-a-2", "a", 150),
            _event("e-b-1", "b", 190),
            _event("e-c-1", "c", 250),
            _event("e-d-1", "d", 300),
            _event("e-a-3", "a", 310),
        )

    def test_input_order_independent_and_summary_is_byte_stable(self) -> None:
        first = cluster_events(self.events, self.topology, 100)
        shuffled = list(self.events)
        random.Random(20261117).shuffle(shuffled)
        second = cluster_events(shuffled, self.topology, 100)
        self.assertEqual(cluster_summary_bytes(first, 100), cluster_summary_bytes(second, 100))
        memberships = [set(cluster.members) for cluster in first]
        self.assertIn({"e-a-1", "e-a-2", "e-b-1", "e-c-1", "e-a-3"}, memberships)
        self.assertIn({"e-d-1"}, memberships)

    def test_same_service_window_bridging(self) -> None:
        clusters = cluster_events(self.events, self.topology, 100)
        a_chain = next(cluster for cluster in clusters if "e-a-1" in cluster.members)
        self.assertIn("e-a-2", a_chain.members)
        self.assertIn("e-a-3", a_chain.members)
        self.assertEqual(a_chain.services, ("a", "b", "c"))

    def test_hash_seed_does_not_change_summary(self) -> None:
        code = (
            "from alarm_domain.models import AlarmEvent,Edge,Topology;"
            "from alarm_domain.clustering import cluster_events,cluster_summary_bytes;"
            "t=Topology(('a','b'),(Edge('a','b'),));"
            "e=(AlarmEvent('e2','R','b',120,1.0),AlarmEvent('e1','R','a',100,1.0));"
            "print(cluster_summary_bytes(cluster_events(e,t,100),100).hex())"
        )
        outputs = []
        for seed in ("1", "99"):
            env = dict(os.environ)
            env["PYTHONHASHSEED"] = seed
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(RULES.parent.parent),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(completed.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])


class RootCauseTests(unittest.TestCase):
    def test_sample_ranking_support_and_propagation_folding(self) -> None:
        session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)
        first = session.analyses[0]
        self.assertEqual(first["root_cause"]["selected_service"], "ledger")
        candidates = first["root_cause"]["candidates"]
        self.assertEqual([candidate["service"] for candidate in candidates][:2], ["ledger", "orders"])
        self.assertEqual(first["root_cause"]["candidates"][0]["support_alerts"], ["e-led-001"])
        folded = {item["event_id"]: item for item in first["deduplicated"]["folded_alerts"]}
        self.assertEqual(folded["e-pay-001"]["folded_into"], "ledger")
        self.assertEqual(folded["e-gw-001"]["folded_into"], "ledger")
        retained = {item["event_id"] for item in first["deduplicated"]["retained_alerts"]}
        self.assertEqual(retained, {"e-led-001", "e-inv-001"})

    def test_explicit_service_name_tie_break(self) -> None:
        topology = Topology(
            services=("beta", "alpha", "gamma"),
            edges=(Edge("gamma", "alpha"), Edge("gamma", "beta")),
        )
        rules = {
            "RB": Rule("RB", "beta", "CRITICAL", {"metric": "m", "operator": ">", "threshold": 0}),
            "RA": Rule("RA", "alpha", "CRITICAL", {"metric": "m", "operator": ">", "threshold": 0}),
            "RG": Rule("RG", "gamma", "INFO", {"metric": "m", "operator": ">", "threshold": 0}),
        }
        events = (
            _event("e-b", "beta", 100, "RB"),
            _event("e-a", "alpha", 100, "RA"),
            _event("e-g", "gamma", 100, "RG"),
        )
        clusters = cluster_events(events, topology, 10)
        analysis = analyze_clusters(clusters, events, rules, topology)[0]
        services = [item["service"] for item in analysis["root_cause"]["candidates"]]
        self.assertEqual(services, ["alpha", "beta", "gamma"])
        self.assertIn("service_name_ascending", analysis["root_cause"]["candidates"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
