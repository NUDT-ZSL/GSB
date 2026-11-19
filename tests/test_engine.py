from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tcp_engine.core import Simulator, assert_consistent
from tcp_engine.errors import (
    E_BAD_LINK,
    E_BAD_SCRIPT,
    E_BAD_TRANSITION,
    E_CLOCK_REWIND,
    E_TIMEOUT_NOT_DUE,
)
from tcp_engine.scene import SceneEvent, load_scene


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def base_parts() -> tuple[dict, dict, dict, dict]:
    return (
        read_json(SAMPLES / "link.json"),
        read_json(SAMPLES / "script.json"),
        read_json(SAMPLES / "congestion.json"),
        read_json(SAMPLES / "observations.json"),
    )


def load_parts(directory: Path, link, script, congestion, observations, name="test"):
    paths = {
        "link": directory / "link.json",
        "script": directory / "script.json",
        "congestion": directory / "congestion.json",
        "observations": directory / "observations.json",
    }
    for key, value in (
        ("link", link),
        ("script", script),
        ("congestion", congestion),
        ("observations", observations),
    ):
        write_json(paths[key], value)
    return load_scene(
        link_path=str(paths["link"]),
        script_path=str(paths["script"]),
        congestion_path=str(paths["congestion"]),
        observations_path=str(paths["observations"]),
        name=name,
    )


class SceneValidationTests(unittest.TestCase):
    def test_numpy_environment(self) -> None:
        import numpy

        self.assertTrue(numpy.__version__)

    def test_combined_and_split_digest_match(self) -> None:
        split = load_scene(
            link_path=str(SAMPLES / "link.json"),
            script_path=str(SAMPLES / "script.json"),
            congestion_path=str(SAMPLES / "congestion.json"),
            observations_path=str(SAMPLES / "observations.json"),
        )
        combined = load_scene(scene_path=str(SAMPLES / "scene.json"))
        self.assertEqual(split.source_digest, combined.source_digest)
        self.assertEqual(len(split.events), 24)

    def test_bad_link_has_business_code_and_no_db(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, script, congestion, observations = base_parts()
            link["rtt_ms"] = 0
            scene_path = directory / "scene.json"
            write_json(
                scene_path,
                {
                    "link": link,
                    "script": script,
                    "congestion": congestion,
                    "observations": observations,
                },
            )
            with self.assertRaises(Exception) as raised:
                load_scene(scene_path=str(scene_path))
            self.assertEqual(raised.exception.code, E_BAD_LINK)
            self.assertFalse((directory / "bad.sqlite").exists())

    def test_unsorted_script_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, script, congestion, observations = base_parts()
            script["events"][2]["ts_ms"] = 5
            with self.assertRaises(Exception) as raised:
                load_parts(directory, link, script, congestion, observations)
            self.assertEqual(raised.exception.code, E_BAD_SCRIPT)

    def test_timeout_future_byte_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, script, congestion, observations = base_parts()
            script = {
                "initial_state": "ESTABLISHED",
                "events": [
                    {
                        "event_id": "timeout-too-far",
                        "ts_ms": 10,
                        "type": "timeout",
                        "seq": 1,
                    }
                ],
            }
            with self.assertRaises(Exception) as raised:
                load_parts(directory, link, script, congestion, observations)
            self.assertEqual(raised.exception.code, E_BAD_SCRIPT)


class StateMachineTests(unittest.TestCase):
    def test_illegal_data_before_establish(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, _, congestion, observations = base_parts()
            script = {
                "initial_state": "LISTEN",
                "events": [
                    {"event_id": "bad-write", "ts_ms": 1, "type": "app_write", "bytes": 10}
                ],
            }
            scene = load_parts(
                directory, link, script, congestion, observations
            )
            simulator = Simulator(scene)
            before = simulator.state.to_dict()
            with self.assertRaises(Exception) as raised:
                simulator.advance(scene.events[0])
            self.assertEqual(raised.exception.code, E_BAD_TRANSITION)
            self.assertEqual(simulator.state.to_dict(), before)

    def test_passive_open_close_wait_and_last_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, _, congestion, observations = base_parts()
            script = {
                "initial_state": "LISTEN",
                "events": [
                    {"event_id": "syn", "ts_ms": 0, "type": "syn_received"},
                    {"event_id": "est", "ts_ms": 10, "type": "establish"},
                    {"event_id": "peer-data", "ts_ms": 20, "type": "peer_data", "bytes": 100},
                    {"event_id": "read-1", "ts_ms": 30, "type": "app_read", "bytes": 50},
                    {"event_id": "peer-fin", "ts_ms": 40, "type": "peer_fin"},
                    {"event_id": "read-2", "ts_ms": 50, "type": "app_read", "bytes": 50},
                    {"event_id": "close", "ts_ms": 60, "type": "app_close"},
                    {"event_id": "fin-ack", "ts_ms": 70, "type": "fin_ack"},
                ],
            }
            scene = load_parts(
                directory, link, script, congestion, observations
            )
            simulator = Simulator(scene)
            states = []
            for event in scene.events:
                result = simulator.advance(event)
                states.append(result["after"]["tcp_state"])
                assert_consistent(scene, result, event)
            self.assertEqual(
                states,
                [
                    "SYN-RECEIVED",
                    "ESTABLISHED",
                    "ESTABLISHED",
                    "ESTABLISHED",
                    "CLOSE-WAIT",
                    "CLOSE-WAIT",
                    "LAST-ACK",
                    "CLOSED",
                ],
            )
            self.assertEqual(simulator.state.peer_data_read, 100)

    def test_clock_rewind_keeps_state(self) -> None:
        scene = load_scene(
            link_path=str(SAMPLES / "link.json"),
            script_path=str(SAMPLES / "script.json"),
            congestion_path=str(SAMPLES / "congestion.json"),
            observations_path=str(SAMPLES / "observations.json"),
        )
        simulator = Simulator(scene)
        simulator.advance(scene.events[0])
        before = simulator.state.to_dict()
        rewind = SceneEvent(
            index=99,
            event_id="rewind",
            ts_ms=0,
            kind="advertised_window",
            payload={"window": 1},
        )
        with self.assertRaises(Exception) as raised:
            simulator.advance(rewind)
        self.assertEqual(raised.exception.code, E_CLOCK_REWIND)
        self.assertEqual(simulator.state.to_dict(), before)

    def test_timeout_before_deadline_keeps_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, _, congestion, observations = base_parts()
            congestion = copy.deepcopy(congestion)
            congestion["initial_rto_ms"] = 200
            script = {
                "initial_state": "ESTABLISHED",
                "events": [
                    {"event_id": "write", "ts_ms": 0, "type": "app_write", "bytes": 4000},
                    {"event_id": "early", "ts_ms": 50, "type": "timeout", "seq": 0},
                ],
            }
            scene = load_parts(
                directory, link, script, congestion, observations
            )
            simulator = Simulator(scene)
            simulator.advance(scene.events[0])
            before = simulator.state.to_dict()
            with self.assertRaises(Exception) as raised:
                simulator.advance(scene.events[1])
            self.assertEqual(raised.exception.code, E_TIMEOUT_NOT_DUE)
            self.assertEqual(simulator.state.to_dict(), before)

    def test_sample_trace_is_byte_deterministic(self) -> None:
        scene = load_scene(
            link_path=str(SAMPLES / "link.json"),
            script_path=str(SAMPLES / "script.json"),
            congestion_path=str(SAMPLES / "congestion.json"),
            observations_path=str(SAMPLES / "observations.json"),
        )
        first = Simulator(scene)
        second = Simulator(scene)
        first_results = [first.advance(event) for event in scene.events]
        second_results = [second.advance(event) for event in scene.events]
        self.assertEqual(
            json.dumps(first_results, sort_keys=True, separators=(",", ":")),
            json.dumps(second_results, sort_keys=True, separators=(",", ":")),
        )
        by_id = {
            result["event"]["event_id"]: result for result in first_results
        }
        self.assertEqual(by_id["dup-ack-6048"]["after"]["cwnd_fixed"], 5120000)
        self.assertEqual(by_id["partial-ack-7000"]["retransmission"]["reason"], "partial_ack_retransmit")
        self.assertEqual(by_id["window-256"]["after"]["receiver_window_bytes"], 256)
        self.assertEqual(by_id["ack-15000"]["after"]["cwnd_fixed"], 3374545)
        self.assertEqual(first_results[-1]["after"]["tcp_state"], "CLOSED")

    def test_timeout_retry_budget_failure_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            link, _, congestion, observations = base_parts()
            congestion = copy.deepcopy(congestion)
            congestion["initial_rto_ms"] = 100
            congestion["max_rto_ms"] = 1000
            congestion["max_retries"] = 1
            script = {
                "initial_state": "ESTABLISHED",
                "events": [
                    {"event_id": "write", "ts_ms": 0, "type": "app_write", "bytes": 4000},
                    {"event_id": "rto-1", "ts_ms": 100, "type": "timeout", "seq": 0},
                    {"event_id": "rto-2", "ts_ms": 300, "type": "timeout", "seq": 0},
                ],
            }
            scene = load_parts(
                directory, link, script, congestion, observations
            )
            simulator = Simulator(scene)
            simulator.advance(scene.events[0])
            first = simulator.advance(scene.events[1])
            self.assertEqual(first["after"]["retry_count"], 1)
            second = simulator.advance(scene.events[2])
            self.assertEqual(second["fatal_error"]["code"], "E_RETRY_EXHAUSTED")
            self.assertEqual(second["after"]["tcp_state"], "FAILED")
            self.assertEqual(second["after"]["retry_count"], 2)


if __name__ == "__main__":
    unittest.main()

