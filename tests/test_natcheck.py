"""Regression tests for the offline adjudication kernel."""

from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from natcheck.diff import (
    compare_normalized_versions,
    compare_result_versions,
    compare_with_reference,
)
from natcheck.engine import adjudicate
from natcheck.errors import SchemaError
from natcheck.generator import build_v1, generate_samples
from natcheck.normalize import load_project, normalize_project
from natcheck.reference import reference_adjudicate
from natcheck.storage import ArtifactStore


def _connection(
    connection_id: str,
    src_port: int,
    timeout: int = 10,
    greenhouse_id: str = "gh-a",
    src_ip: str = "10.0.0.1",
    dst_port: int = 8000,
) -> dict:
    return {
        "connection_id": connection_id,
        "greenhouse_id": greenhouse_id,
        "direction": "inbound",
        "tracking_timeout": timeout,
        "five_tuple": {
            "src_ip": src_ip,
            "src_port": src_port,
            "dst_ip": "198.51.100.1",
            "dst_port": dst_port,
            "protocol": "tcp",
        },
    }


def _rule(rule_id: str, priority: int, pool_id: str = "p-a") -> dict:
    return {
        "rule_id": rule_id,
        "greenhouse_id": "gh-a",
        "priority": [priority],
        "protocol": "tcp",
        "direction": "inbound",
        "src_cidr": "10.0.0.0/24",
        "dst_port_start": 8000,
        "dst_port_end": 8100,
        "src_port_start": None,
        "src_port_end": None,
        "pool_id": pool_id,
    }


def _pool(
    pool_id: str = "p-a",
    start: int = 50000,
    end: int = 50010,
    external_ip: str = "203.0.113.10",
) -> dict:
    return {
        "pool_id": pool_id,
        "greenhouse_id": "gh-a",
        "external_ip": external_ip,
        "port_start": start,
        "port_end": end,
    }


def _model(
    connections: list[dict],
    events: list[dict],
    rules: list[dict] | None = None,
    pools: list[dict] | None = None,
    horizon: int = 20,
) -> dict:
    return {
        "canonical_schema": "natcheck-canonical/1",
        "source_schema": "v1",
        "project_id": "test-project",
        "domain_id": "test-domain",
        "horizon": horizon,
        "connections": connections,
        "rules": rules if rules is not None else [_rule("r-a", 10)],
        "pools": pools if pools is not None else [_pool()],
        "events": sorted(events, key=lambda item: (item["time"], item["seq"])),
        "provenance": [],
    }


def _event(
    event_id: str,
    time: int,
    seq: int,
    event_type: str,
    connection_id: str | None = None,
) -> dict:
    result = {
        "event_id": event_id,
        "time": time,
        "seq": seq,
        "type": event_type,
    }
    if connection_id is not None:
        result["connection_id"] = connection_id
    return result


class SchemaTests(unittest.TestCase):
    def test_missing_required_field_is_e_schema(self) -> None:
        raw = build_v1()
        del raw["connections"][0]["tracking_timeout"]
        with self.assertRaises(SchemaError) as caught:
            normalize_project(raw)
        self.assertEqual(caught.exception.as_dict()["code"], "E_SCHEMA")
        self.assertTrue(
            any(
                issue["entity_id"] == "c1"
                and issue["field"] == "tracking_timeout"
                for issue in caught.exception.as_dict()["issues"]
            )
        )

    def test_out_of_range_decimal_port_is_e_schema(self) -> None:
        raw = build_v1()
        raw["connections"][0]["src_port"] = 70000
        with self.assertRaises(SchemaError) as caught:
            normalize_project(raw)
        issues = caught.exception.as_dict()["issues"]
        self.assertTrue(any(issue["field"] == "src_port" for issue in issues))

    def test_out_of_range_base_offset_port_is_e_schema(self) -> None:
        raw = copy.deepcopy(build_v1())
        raw["schema_version"] = "v3"
        raw["version"] = "v3"
        raw["meta"] = {
            "project": raw["project_id"],
            "domain": raw["domain_id"],
            "horizon": raw["horizon"],
        }
        raw["conns"] = [
            {
                "id": "c1",
                "greenhouse": "gh-a",
                "src_ip": "10.20.1.101",
                "dst_ip": "198.51.100.20",
                "src_port": {"base": 65000, "offset": 1000},
                "dst_port": {"base": 8000, "offset": 0},
                "protocol": "tcp",
                "direction": "inbound",
                "timeout": 50,
            }
        ]
        raw["map_rules"] = [
            {
                "id": "r-in-a",
                "greenhouse": "gh-a",
                "priority_path": "zone.edge.10",
                "protocol": "tcp",
                "direction": "inbound",
                "src_cidr": "10.20.1.0/24",
                "dst_port_base": 8000,
                "dst_offset_start": 0,
                "dst_offset_end": 10,
                "pool": "p-a",
            }
        ]
        raw["pools"] = [
            {
                "id": "p-a",
                "greenhouse": "gh-a",
                "external_ip": "203.0.113.10",
                "port_base": 50000,
                "offset_start": 0,
                "offset_end": 2,
            }
        ]
        raw["log"] = []
        with self.assertRaises(SchemaError) as caught:
            normalize_project(raw)
        issues = caught.exception.as_dict()["issues"]
        self.assertTrue(
            any("out of range" in issue["reason"] for issue in issues)
        )


class NormalizationTests(unittest.TestCase):
    def test_three_versions_normalize_identically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = generate_samples(temp)
            models = {
                version: load_project(path)
                for version, path in paths.items()
            }
        comparison = compare_normalized_versions(models)
        self.assertTrue(comparison["equal"])
        self.assertFalse(comparison["semantic_differences"])
        self.assertTrue(comparison["representation_differences"])
        self.assertTrue(
            any(
                item["field"] == "five_tuple.src_port"
                and item["normalized_equal"]
                for item in comparison["representation_differences"]
            )
        )


class StateMachineTests(unittest.TestCase):
    def test_same_time_teardown_and_new_are_ordered_by_seq(self) -> None:
        model = _model(
            [_connection("c1", 1000, timeout=100)],
            [
                _event("new-1", 0, 1, "new", "c1"),
                _event("est-1", 1, 1, "established", "c1"),
                _event("down-1", 2, 1, "teardown", "c1"),
                _event("new-2", 2, 2, "new", "c1"),
                _event("est-2", 3, 1, "established", "c1"),
            ],
            horizon=4,
        )
        result = adjudicate(model)
        self.assertEqual([item["session_id"] for item in result["sessions"]], ["c1#1", "c1#2"])
        self.assertEqual(result["sessions"][0]["state"], "teardown")
        self.assertEqual(result["sessions"][1]["state"], "established")

    def test_timeout_then_same_tuple_creates_new_session(self) -> None:
        model = _model(
            [_connection("c1", 1000, timeout=3)],
            [
                _event("new-1", 0, 1, "new", "c1"),
                _event("est-1", 1, 1, "established", "c1"),
                _event("tick-4", 4, 1, "tick"),
                _event("new-2", 4, 2, "new", "c1"),
                _event("est-2", 5, 1, "established", "c1"),
            ],
            horizon=6,
        )
        result = adjudicate(model)
        first, second = result["sessions"]
        self.assertEqual(first["state"], "timeout")
        self.assertEqual(first["terminal_at"], 4)
        self.assertEqual(second["state"], "established")
        self.assertEqual(len(result["residual_cleanups"]), 1)
        self.assertEqual(result["pools"][0]["occupied_count"], 1)
        self.assertEqual(
            result["pools"][0]["occupied"][0]["session_id"], "c1#2"
        )

    def test_timeout_and_teardown_competition_uses_seq(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = load_project(generate_samples(temp)["v1"])
        result = adjudicate(model)
        c5 = next(item for item in result["sessions"] if item["session_id"] == "c5#1")
        c6 = next(item for item in result["sessions"] if item["session_id"] == "c6#1")
        self.assertEqual(c5["state"], "timeout")
        self.assertEqual(c6["state"], "teardown")
        self.assertEqual(c5["trace"][-1]["time"], 8)
        self.assertEqual(c6["trace"][-1]["time"], 8)


class MappingTests(unittest.TestCase):
    def test_priority_tie_uses_rule_id_not_input_order(self) -> None:
        connection = _connection("c1", 1000)
        rules_a = [_rule("r-a", 10, "p-a"), _rule("r-b", 10, "p-b")]
        rules_b = list(reversed(rules_a))
        pools = [
            _pool("p-a", 50000, 50000),
            _pool("p-b", 50010, 50010, "203.0.113.11"),
        ]
        first = adjudicate(
            _model(
                [connection],
                [_event("new", 0, 1, "new", "c1")],
                rules_a,
                pools,
            )
        )
        second = adjudicate(
            _model(
                [connection],
                [_event("new", 0, 1, "new", "c1")],
                rules_b,
                pools,
            )
        )
        self.assertEqual(first["sessions"][0]["mapping"]["rule_id"], "r-a")
        self.assertEqual(
            first["sessions"][0]["mapping"],
            second["sessions"][0]["mapping"],
        )

    def test_pool_shrink_only_rebuilds_sessions_outside_new_range(self) -> None:
        model = _model(
            [_connection("c1", 1000), _connection("c2", 1001)],
            [
                _event("new-1", 0, 1, "new", "c1"),
                _event("new-2", 0, 2, "new", "c2"),
                _event("est-1", 1, 1, "established", "c1"),
                _event("est-2", 1, 2, "established", "c2"),
                {
                    "event_id": "shrink",
                    "time": 2,
                    "seq": 1,
                    "type": "policy_change",
                    "operation": "upsert_pool",
                    "pool_id": "p-a",
                    "pool_object": _pool("p-a", 50001, 50002),
                },
            ],
            pools=[_pool("p-a", 50000, 50002)],
        )
        result = adjudicate(model)
        c1 = next(item for item in result["sessions"] if item["session_id"] == "c1#1")
        c2 = next(item for item in result["sessions"] if item["session_id"] == "c2#1")
        self.assertEqual(c1["mapping"]["external_port"], 50002)
        self.assertEqual(c2["mapping"]["external_port"], 50001)
        self.assertTrue(c1["require_rebuild"])
        self.assertFalse(c2["require_rebuild"])
        impact = result["impacts"][0]
        self.assertEqual(
            [item["session_id"] for item in impact["affected"]],
            ["c1#1"],
        )
        self.assertEqual(impact["unaffected_sessions"], ["c2#1"])


class ReferenceTests(unittest.TestCase):
    def test_reference_matches_all_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = generate_samples(temp)
            models = {version: load_project(path) for version, path in paths.items()}
        results = {version: adjudicate(model) for version, model in models.items()}
        self.assertTrue(compare_result_versions(results)["equal"])
        for version, model in models.items():
            check = compare_with_reference(
                results[version], reference_adjudicate(model)
            )
            self.assertTrue(check["ok"], version)

    def test_first_mismatch_identifies_connection_event_and_basis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            model = load_project(generate_samples(temp)["v1"])
        primary = adjudicate(model)
        reference = reference_adjudicate(model)
        broken = copy.deepcopy(primary)
        session = next(item for item in broken["sessions"] if item["session_id"] == "c4#1")
        session["mapping"]["external_port"] = 59999
        check = compare_with_reference(broken, reference)
        self.assertFalse(check["ok"])
        mismatch = check["first_mismatch"]
        self.assertEqual(mismatch["session_id"], "c4#1")
        self.assertEqual(mismatch["connection_id"], "c4")
        self.assertIn("external_port", mismatch["path"])


class StorageTests(unittest.TestCase):
    def test_sqlite_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            paths = generate_samples(Path(temp) / "samples")
            model = load_project(paths["v1"])
            primary = adjudicate(model)
            reference = reference_adjudicate(model)
            check = compare_with_reference(primary, reference)
            db = Path(temp) / "results.sqlite3"
            with ArtifactStore(db) as store:
                run_id = store.store_run(
                    model,
                    primary,
                    reference,
                    check,
                    source_path=paths["v1"],
                    source_sha256="a" * 64,
                )
                loaded = store.get_result(run_id)
                counts = store.counts()
            self.assertEqual(loaded["summary"], primary["summary"])
            self.assertEqual(counts["projects"], 1)
            self.assertEqual(counts["sessions"], 9)
            with sqlite3.connect(db) as connection:
                row = connection.execute(
                    "SELECT final_state FROM sessions "
                    "WHERE run_id=? AND session_id=?",
                    (run_id, "c4#1"),
                ).fetchone()
            self.assertEqual(row[0], "established")


if __name__ == "__main__":
    unittest.main()

