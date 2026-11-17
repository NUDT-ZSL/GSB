from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from alarm_domain.errors import DomainError
from alarm_domain.loading import (
    load_events,
    load_inputs,
    load_rules,
    load_topology,
    validate_consistency,
)

from .helpers import EVENTS, RULES, TOPOLOGY


class LoadingTests(unittest.TestCase):
    def test_valid_samples_load_strictly(self) -> None:
        rules, topology, events = load_inputs(RULES, TOPOLOGY, EVENTS)
        self.assertEqual(len(rules), 9)
        self.assertEqual(len(topology.services), 7)
        self.assertEqual(len(topology.edges), 5)
        self.assertEqual(len(events), 10)
        self.assertEqual([event.timestamp for event in events], sorted(event.timestamp for event in events))

    def test_duplicate_rule_id_is_whole_input_error(self) -> None:
        document = json.loads(RULES.read_text(encoding="utf-8"))
        document["rules"].append(dict(document["rules"][0]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-rules.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(DomainError) as caught:
                load_rules(path)
        self.assertEqual(caught.exception.code, "E_SPEC")
        self.assertEqual(caught.exception.file, "bad-rules.json")
        self.assertGreaterEqual(caught.exception.line or 0, 1)

    def test_unsupported_severity_rejected(self) -> None:
        document = json.loads(RULES.read_text(encoding="utf-8"))
        document["rules"][0]["severity"] = "FATAL"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-severity.json"
            path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            with self.assertRaises(DomainError) as caught:
                load_rules(path)
        self.assertEqual(caught.exception.code, "E_SPEC")
        self.assertEqual(caught.exception.detail["severity"], "FATAL")

    def test_unknown_topology_endpoint_rejected(self) -> None:
        document = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
        document["edges"].append(
            {"upstream": "ghost", "downstream": "orders", "weight": 1.0}
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-topology.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(DomainError) as caught:
                load_topology(path)
        self.assertEqual(caught.exception.code, "E_SPEC")
        self.assertEqual(caught.exception.detail["missing_services"], ["ghost"])

    def test_rule_topology_service_sets_must_match(self) -> None:
        rules = load_rules(RULES)
        topology_doc = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
        topology_doc["services"].append("orphan")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orphan-topology.json"
            path.write_text(json.dumps(topology_doc), encoding="utf-8")
            topology = load_topology(path)
        with self.assertRaises(DomainError) as caught:
            validate_consistency(rules, topology)
        self.assertEqual(caught.exception.code, "E_SPEC")
        self.assertEqual(caught.exception.detail["topology_only_services"], ["orphan"])

    def test_timestamp_regression_uses_e_time_with_line(self) -> None:
        rules = load_rules(RULES)
        lines = EVENTS.read_text(encoding="utf-8").splitlines()
        item = json.loads(lines[3])
        item["timestamp"] = 1100
        lines[3] = json.dumps(item, separators=(",", ":"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "regression.ndjson"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(DomainError) as caught:
                load_events(path, rules)
        self.assertEqual(caught.exception.code, "E_TIME")
        self.assertEqual(caught.exception.file, "regression.ndjson")
        self.assertEqual(caught.exception.line, 4)

    def test_non_finite_and_service_mismatch_are_e_spec(self) -> None:
        rules = load_rules(RULES)
        original = EVENTS.read_text(encoding="utf-8").splitlines()
        for replacement, expected_fragment in (
            ({"event_id": "bad", "rule_id": "R-GW-5XX", "service": "gateway", "timestamp": 13000, "value": float("inf")}, "finite"),
            ({"event_id": "bad", "rule_id": "R-GW-5XX", "service": "gateway", "timestamp": 13000, "value": 10 ** 1000}, "finite"),
            ({"event_id": "bad", "rule_id": "R-GW-5XX", "service": "orders", "timestamp": 13000, "value": 1.0}, "does not match"),
        ):
            with self.subTest(expected_fragment=expected_fragment):
                lines = original + [json.dumps(replacement)]
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "bad-event.ndjson"
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    with self.assertRaises(DomainError) as caught:
                        load_events(path, rules)
                self.assertEqual(caught.exception.code, "E_SPEC")
                self.assertIn(expected_fragment, caught.exception.message)


if __name__ == "__main__":
    unittest.main()
