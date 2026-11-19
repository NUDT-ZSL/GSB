from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from h2diag.engine import Engine
from h2diag.errors import (
    E_BAD_SCENARIO,
    E_FINAL_STATE,
    E_FLOW_CONTROL,
    E_GOAWAY_STREAM,
    E_HEADERS_INCOMPLETE,
    E_HEADERS_SEQUENCE,
    E_STREAM_STATE,
    E_TRAJECTORY_MISMATCH,
    E_UNKNOWN_STREAM,
    E_WINDOW_OVERFLOW,
    EngineError,
    ScenarioError,
    VerificationError,
    WorkbenchError,
)
from h2diag.runner import (
    preflight,
    resume_scenario,
    run_scenario,
    verify_run,
)
from h2diag.sample import build_sample_scenario, write_sample_scenario
from h2diag.scenario import parse_scenario
from h2diag.storage import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]


def parse(raw: dict) -> object:
    return parse_scenario(copy.deepcopy(raw))


def minimal_scenario(
    *,
    streams: list[dict] | None = None,
    events: list[dict] | None = None,
    connection: dict | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "scenario_id": "minimal",
        "initiator": "client",
        "connection": connection
        or {
            "send_window": 2000,
            "recv_window": 2000,
            "max_window": 5000,
            "max_concurrent_streams": 4,
        },
        "streams": streams
        or [
            {
                "stream_id": 1,
                "initial_send_window": 5000,
                "initial_recv_window": 5000,
                "weight": 1,
                "expected_final_state": "closed",
            }
        ],
        "events": events or [],
    }


def event(seq: int, at_ms: int, **values) -> dict:
    result = {"seq": seq, "at_ms": at_ms}
    result.update(values)
    return result


class ScenarioValidationTests(unittest.TestCase):
    def assert_error(self, raw: dict, code: str) -> None:
        with self.assertRaises(WorkbenchError) as caught:
            preflight(parse(raw))
        self.assertEqual(caught.exception.code, code)

    def test_unknown_stream_is_rejected_before_database_open(self) -> None:
        raw = build_sample_scenario()
        raw["events"][4]["stream_id"] = 99
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "invalid.db"
            with self.assertRaises(WorkbenchError) as caught:
                run_scenario(parse(raw), str(db), "bad")
            self.assertEqual(caught.exception.code, E_UNKNOWN_STREAM)
            self.assertFalse(db.exists())

    def test_stream_parity_and_monotonic_ids(self) -> None:
        raw = build_sample_scenario()
        raw["streams"][0]["stream_id"] = 2
        self.assert_error(raw, E_BAD_SCENARIO)

        raw = build_sample_scenario()
        raw["streams"][1]["stream_id"] = 1
        self.assert_error(raw, E_BAD_SCENARIO)

    def test_logical_clock_cannot_regress(self) -> None:
        raw = build_sample_scenario()
        raw["events"][2]["at_ms"] = 0
        self.assert_error(raw, E_BAD_SCENARIO)

    def test_window_and_weight_limits(self) -> None:
        raw = build_sample_scenario()
        raw["streams"][0]["weight"] = 257
        self.assert_error(raw, E_BAD_SCENARIO)

        raw = build_sample_scenario()
        raw["streams"][0]["initial_send_window"] = 0
        self.assert_error(raw, E_BAD_SCENARIO)

    def test_goaway_last_stream_id_out_of_range(self) -> None:
        raw = build_sample_scenario()
        goaway = next(item for item in raw["events"] if item["type"] == "GOAWAY")
        goaway["last_stream_id"] = 11
        self.assert_error(raw, E_BAD_SCENARIO)

    def test_invalid_transition_writes_nothing(self) -> None:
        raw = build_sample_scenario()
        raw["events"].append(
            event(
                27,
                100,
                direction="inbound",
                end_stream=False,
                length=1,
                stream_id=1,
                type="DATA",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "revive.db"
            with self.assertRaises(EngineError) as caught:
                run_scenario(parse(raw), str(db), "bad")
            self.assertEqual(caught.exception.code, E_STREAM_STATE)
            self.assertFalse(db.exists())

    def test_idle_rst_stream_is_rejected(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "idle",
                }
            ],
            events=[
                event(
                    1,
                    0,
                    direction="inbound",
                    error_code=8,
                    stream_id=1,
                    type="RST_STREAM",
                )
            ],
        )
        self.assert_error(raw, E_STREAM_STATE)

    def test_header_sequence_and_incomplete_rules(self) -> None:
        raw = build_sample_scenario()
        raw["events"][4]["end_headers"] = True
        self.assert_error(raw, E_HEADERS_SEQUENCE)

        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "open",
                }
            ],
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=False,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                )
            ],
        )
        self.assert_error(raw, E_HEADERS_INCOMPLETE)

    def test_window_update_overflow_is_rejected(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "open",
                }
            ],
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                ),
                event(
                    2,
                    1,
                    direction="inbound",
                    increment=1000,
                    level="connection",
                    type="WINDOW_UPDATE",
                ),
            ],
            connection={
                "send_window": 100,
                "recv_window": 1000,
                "max_window": 1000,
                "max_concurrent_streams": 4,
            },
        )
        self.assert_error(raw, E_WINDOW_OVERFLOW)

    def test_final_state_mismatch_is_rejected(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "closed",
                }
            ],
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                )
            ],
        )
        self.assert_error(raw, E_FINAL_STATE)


class EngineBehaviorTests(unittest.TestCase):
    def test_weighted_allocation_is_bitwise_reproducible(self) -> None:
        scenario = parse(build_sample_scenario())
        first = preflight(scenario)
        second = preflight(scenario)
        first_signature = [
            (
                step.outcome.allocations,
                step.outcome.state_digest,
                step.ledger_digest,
                step.trajectory_digest,
            )
            for step in first.steps
        ]
        second_signature = [
            (
                step.outcome.allocations,
                step.outcome.state_digest,
                step.ledger_digest,
                step.trajectory_digest,
            )
            for step in second.steps
        ]
        self.assertEqual(first_signature, second_signature)
        self.assertEqual(first.final_state_digest, second.final_state_digest)
        self.assertEqual(first.final_trajectory_digest, second.final_trajectory_digest)
        self.assertEqual(first.final_ledger_digest, second.final_ledger_digest)
        self.assertTrue(
            any(len(step.outcome.allocations) > 1 for step in first.steps)
        )

    def test_reset_recycles_only_unacknowledged_connection_credit(self) -> None:
        raw = minimal_scenario(
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                ),
                event(
                    2,
                    1,
                    direction="outbound",
                    end_stream=False,
                    length=1000,
                    stream_id=1,
                    type="DATA",
                ),
                event(
                    3,
                    2,
                    direction="inbound",
                    error_code=8,
                    stream_id=1,
                    type="RST_STREAM",
                ),
            ]
        )
        simulation = preflight(parse(raw))
        connection = simulation.final_state.connection
        self.assertEqual(connection.send_window, 2000)
        self.assertEqual(connection.unacked_send, 0)
        self.assertEqual(connection.reset_reclaimed, 1000)
        self.assertEqual(simulation.final_state.streams[1].state, "closed")
        self.assertEqual(simulation.final_state.streams[1].unacked_conn, 0)

    def test_goaway_abandons_streams_above_last_stream_id(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "closed",
                },
                {
                    "stream_id": 3,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "error",
                },
            ],
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                ),
                event(
                    2,
                    1,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="01",
                    stream_id=3,
                    type="HEADERS",
                ),
                event(
                    3,
                    2,
                    direction="inbound",
                    error_code=0,
                    last_stream_id=1,
                    type="GOAWAY",
                ),
                event(
                    4,
                    3,
                    direction="outbound",
                    end_stream=True,
                    length=0,
                    stream_id=1,
                    type="DATA",
                ),
                event(
                    5,
                    4,
                    direction="inbound",
                    end_stream=True,
                    length=0,
                    stream_id=1,
                    type="DATA",
                ),
            ],
        )
        simulation = preflight(parse(raw))
        self.assertEqual(simulation.final_state.streams[1].state, "closed")
        self.assertEqual(simulation.final_state.streams[3].state, "error")
        self.assertEqual(
            simulation.final_state.streams[3].terminal_reason,
            "goaway_not_processed",
        )

    def test_goaway_blocks_new_stream_above_last_stream_id(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "open",
                },
                {
                    "stream_id": 3,
                    "initial_send_window": 1000,
                    "initial_recv_window": 1000,
                    "weight": 1,
                    "expected_final_state": "closed",
                },
            ],
            events=[
                event(
                    1,
                    0,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                ),
                event(
                    2,
                    1,
                    direction="inbound",
                    error_code=0,
                    last_stream_id=1,
                    type="GOAWAY",
                ),
                event(
                    3,
                    2,
                    direction="outbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="01",
                    stream_id=3,
                    type="HEADERS",
                ),
            ],
        )
        with self.assertRaises(WorkbenchError) as caught:
            preflight(parse(raw))
        self.assertEqual(caught.exception.code, E_GOAWAY_STREAM)

    def test_inbound_data_consumes_both_receive_windows(self) -> None:
        raw = minimal_scenario(
            streams=[
                {
                    "stream_id": 1,
                    "initial_send_window": 1000,
                    "initial_recv_window": 400,
                    "weight": 1,
                    "expected_final_state": "closed",
                }
            ],
            events=[
                event(
                    1,
                    0,
                    direction="inbound",
                    end_headers=True,
                    end_stream=False,
                    fragment_hex="00",
                    stream_id=1,
                    type="HEADERS",
                ),
                event(
                    2,
                    1,
                    direction="inbound",
                    end_stream=True,
                    length=400,
                    stream_id=1,
                    type="DATA",
                ),
                event(
                    3,
                    2,
                    direction="outbound",
                    end_stream=True,
                    length=0,
                    stream_id=1,
                    type="DATA",
                ),
            ],
        )
        simulation = preflight(parse(raw))
        self.assertEqual(simulation.final_state.connection.recv_window, 1600)
        self.assertEqual(simulation.final_state.streams[1].recv_window, 0)

        raw["events"][1]["length"] = 401
        with self.assertRaises(EngineError) as caught:
            preflight(parse(raw))
        self.assertEqual(caught.exception.code, E_FLOW_CONTROL)


class PersistenceAndRecoveryTests(unittest.TestCase):
    def test_clean_run_verify_and_idempotent_resume(self) -> None:
        scenario = parse(build_sample_scenario())
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "run.db"
            status = run_scenario(scenario, str(db), "fixed")
            self.assertEqual(status["run"]["status"], "complete")
            report = verify_run(scenario, str(db), "fixed")
            self.assertTrue(report["ok"])
            before = status
            resumed = resume_scenario(scenario, str(db), "fixed")
            self.assertEqual(before, resumed)
            store = SQLiteStore(db)
            try:
                store.open(create=False)
                self.assertEqual(
                    len(store.load_snapshots("fixed")),
                    len(scenario.events) + 1,
                )
            finally:
                store.close()

    def test_forced_half_commit_recovery_matches_clean_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            scenario_path = directory_path / "scenario.json"
            write_sample_scenario(scenario_path)
            clean_db = directory_path / "clean.db"
            crash_db = directory_path / "crash.db"
            clean_trace = directory_path / "clean.jsonl"
            crash_trace = directory_path / "crash.jsonl"
            scenario = parse(build_sample_scenario())

            with clean_trace.open("w", encoding="utf-8", newline="\n") as handle:
                clean_status = run_scenario(
                    scenario,
                    str(clean_db),
                    "same-run",
                    trace_handle=handle,
                )
            verify_run(scenario, str(clean_db), "same-run")

            common = [
                sys.executable,
                "-m",
                "h2diag",
                "run",
                "--scenario",
                str(scenario_path),
                "--db",
                str(crash_db),
                "--run-id",
                "same-run",
                "--trace",
                str(crash_trace),
                "--crash-before-commit",
                "15",
            ]
            crashed = subprocess.run(
                common,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(crashed.returncode, 91, crashed.stderr)

            store = SQLiteStore(crash_db)
            try:
                store.open(create=False)
                run = store.load_run("same-run")
                self.assertEqual(run["committed_seq"], 14)
                self.assertEqual(run["status"], "running")
            finally:
                store.close()

            with crash_trace.open("a", encoding="utf-8", newline="\n") as handle:
                resumed_status = resume_scenario(
                    scenario,
                    str(crash_db),
                    "same-run",
                    trace_handle=handle,
                )
            report = verify_run(scenario, str(crash_db), "same-run")
            self.assertTrue(report["ok"])
            self.assertEqual(clean_status, resumed_status)
            self.assertEqual(
                clean_status["run"]["state_digest"],
                resumed_status["run"]["state_digest"],
            )
            self.assertEqual(
                clean_status["run"]["trajectory_digest"],
                resumed_status["run"]["trajectory_digest"],
            )
            self.assertEqual(
                clean_status["run"]["ledger_digest"],
                resumed_status["run"]["ledger_digest"],
            )
            self.assertEqual(clean_trace.read_bytes(), crash_trace.read_bytes())

    def test_verify_detects_tampered_snapshot(self) -> None:
        scenario = parse(build_sample_scenario())
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "tampered.db"
            run_scenario(scenario, str(db), "fixed")
            connection = sqlite3.connect(str(db))
            try:
                row = connection.execute(
                    "SELECT state_json FROM snapshots "
                    "WHERE run_id='fixed' AND seq=10"
                ).fetchone()
                payload = json.loads(row[0])
                payload["logical_clock_ms"] = 999999
                connection.execute(
                    "UPDATE snapshots SET state_json=? "
                    "WHERE run_id='fixed' AND seq=10",
                    (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(VerificationError) as caught:
                verify_run(scenario, str(db), "fixed")
            self.assertEqual(caught.exception.code, E_TRAJECTORY_MISMATCH)


if __name__ == "__main__":
    unittest.main()

