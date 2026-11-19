from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from elastic_control.engine import Controller, plan_backoff
from elastic_control.errors import ErrorCode
from elastic_control.launcher import HTTPControlClient
from elastic_control.models import load_input_bundle
from elastic_control.policy import parse_policy
from elastic_control.store import Store

from .support import (
    ROOT,
    SAMPLES,
    mini_inputs,
    running_server,
    server_state,
    stable_dump,
)


def sample_bundle():
    return load_input_bundle(
        SAMPLES / "services.json",
        SAMPLES / "nodes.json",
        SAMPLES / "trace.json",
    )


def make_controller(
    root: Path,
    url: str,
    *,
    bundle=None,
    budget: int = 8,
    timeout: float = 1.0,
) -> Controller:
    store = Store(root / "state.db", root / "runtime")
    store.initialize()
    return Controller(
        bundle=bundle or sample_bundle(),
        policy=parse_policy(
            {
                "logical_time_budget_ticks": budget,
                "action_timeout_seconds": timeout,
            }
        ),
        store=store,
        launcher=HTTPControlClient(url),
    )


def run_worker(
    *,
    script: str,
    services: Path,
    nodes: Path,
    trace: Path,
    db: Path,
    runtime: Path,
    url: str,
    round_id: str,
    tick: int,
    service: str,
    out: Path | None = None,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(ROOT / "tests" / script),
        "--services",
        str(services),
        "--nodes",
        str(nodes),
        "--trace",
        str(trace),
        "--db",
        str(db),
        "--runtime-dir",
        str(runtime),
        "--url",
        url,
        "--round-id",
        round_id,
        "--tick",
        str(tick),
        "--service",
        service,
        "--budget",
        "8",
    ]
    if out is not None:
        command.extend(["--out", str(out)])
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class EngineTests(unittest.TestCase):
    def test_round_is_idempotent_and_phases_advance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            server,
            url,
        ):
            root = Path(temporary)
            controller = make_controller(root, url)
            first = controller.run_round(round_id="r0", tick=0)
            second = controller.run_round(round_id="r0", tick=0)
            self.assertEqual(stable_dump(first), stable_dump(second))
            self.assertEqual(server.fault_state.snapshot()["request_count"], 3)
            self.assertEqual(first["outcome_counts"]["started"], 3)
            self.assertEqual(
                first["replica_phases"]["chat-alpha"],
                {"cold_starting": 1},
            )

            next_round = controller.run_round(round_id="r1", tick=1)
            self.assertEqual(
                next_round["replica_phases"]["embed-beta"].get("ready"), 2
            )
            snapshot = controller.store.get_current_snapshot()
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["version"], 2)

    def test_budget_and_per_service_concurrency_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            _,
            url,
        ):
            root = Path(temporary)
            controller = make_controller(root, url, budget=2)
            result = controller.run_round(round_id="budget", tick=1)
            counts = result["outcome_counts"]
            self.assertEqual(counts["started"], 1)
            self.assertGreater(counts["budget_not_started"], 0)
            decisions = [
                decision
                for service in result["services"]
                for decision in service["decisions"]
            ]
            not_started = [
                item
                for item in decisions
                if item["outcome_class"] == "budget_not_started"
            ]
            self.assertTrue(not_started)
            self.assertTrue(
                all(item["reason_code"] == "budget_exhausted" for item in not_started)
            )
            order_keys = [item["detail"]["priority"]["order_key"] for item in decisions]
            self.assertEqual(order_keys, sorted(order_keys))

        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            _,
            url,
        ):
            root = Path(temporary)
            controller = make_controller(root, url, budget=8)
            result = controller.run_round(round_id="cap", tick=1)
            for service in result["services"]:
                selected = [
                    decision
                    for decision in service["decisions"]
                    if decision["kind"] == "launch"
                    and decision["outcome_class"] == "started"
                ]
                self.assertLessEqual(len(selected), 2)
            self.assertGreater(result["outcome_counts"]["abandoned"], 0)

    def test_launch_error_codes_are_distinct_and_isolated(self) -> None:
        cases = (
            ("version_mismatch", ErrorCode.REPLICA_VERSION_MISMATCH),
            ("digest_mismatch", ErrorCode.SAMPLE_DIGEST_MISMATCH),
            ("http_500", ErrorCode.LAUNCH_HTTP_ERROR),
            ("remote_error", ErrorCode.LAUNCH_REMOTE_ERROR),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode):
                fault = {
                    "rules": [
                        {
                            "match": {
                                "action": "launch",
                                "service_name": "chat-alpha",
                            },
                            "times": 1,
                            "mode": mode,
                        }
                    ]
                }
                with tempfile.TemporaryDirectory() as temporary, running_server(
                    fault
                ) as (_, url):
                    root = Path(temporary)
                    controller = make_controller(root, url)
                    result = controller.run_round(round_id=mode, tick=0)
                    by_service = {
                        service["service_name"]: service
                        for service in result["services"]
                    }
                    chat_actions = by_service["chat-alpha"]["actions"]
                    embed_actions = by_service["embed-beta"]["actions"]
                    self.assertEqual(chat_actions[0]["status"], "failed")
                    self.assertEqual(chat_actions[0]["error_code"], expected.value)
                    self.assertTrue(embed_actions)
                    self.assertTrue(
                        all(action["status"] == "succeeded" for action in embed_actions)
                    )

    def test_timeout_and_connection_refused_have_distinct_codes(self) -> None:
        fault = {
            "rules": [
                {
                    "match": {"action": "launch", "service_name": "chat-alpha"},
                    "times": 1,
                    "delay_ms": 250,
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary, running_server(fault) as (
            _,
            url,
        ):
            root = Path(temporary)
            controller = make_controller(root, url, timeout=0.03)
            result = controller.run_round(
                round_id="timeout",
                tick=0,
                service_filter=["chat-alpha"],
            )
            action = result["services"][0]["actions"][0]
            self.assertEqual(action["error_code"], ErrorCode.LAUNCH_TIMEOUT.value)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        refused_port = sock.getsockname()[1]
        sock.close()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = make_controller(
                root,
                f"http://127.0.0.1:{refused_port}",
                timeout=0.2,
            )
            result = controller.run_round(
                round_id="refused",
                tick=0,
                service_filter=["chat-alpha"],
            )
            action = result["services"][0]["actions"][0]
            self.assertEqual(
                action["error_code"],
                ErrorCode.LAUNCH_CONNECTION_REFUSED.value,
            )

    def test_backoff_is_deterministic_and_success_resets_failures(self) -> None:
        fault = {
            "rules": [
                {
                    "match": {
                        "action": "launch",
                        "service_name": "chat-alpha",
                        "replica_id": "chat-alpha--0001",
                    },
                    "times": 1,
                    "mode": "http_500",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temporary, running_server(fault) as (
            _,
            url,
        ):
            root = Path(temporary)
            controller = make_controller(root, url)
            failed = controller.run_round(
                round_id="backoff-0",
                tick=0,
                service_filter=["chat-alpha"],
            )
            action = next(
                item
                for item in failed["services"][0]["actions"]
                if item["replica_id"] == "chat-alpha--0001"
            )
            self.assertEqual(action["status"], "failed")
            with controller.store.read() as connection:
                replica = dict(
                    connection.execute(
                        "SELECT * FROM replicas WHERE replica_id = ?",
                        ("chat-alpha--0001",),
                    ).fetchone()
                )
            expected = plan_backoff(
                replica_id="chat-alpha--0001",
                failure_count=1,
                tick=0,
                policy=controller.policy,
            )
            self.assertEqual(replica["failure_count"], 1)
            self.assertEqual(replica["next_retry_tick"], expected.next_retry_tick)
            retry_tick = int(replica["next_retry_tick"])
            result = controller.run_round(
                round_id=f"backoff-{retry_tick}",
                tick=retry_tick,
                service_filter=["chat-alpha"],
            )
            retry_action = next(
                item
                for item in result["services"][0]["actions"]
                if item["replica_id"] == "chat-alpha--0001"
            )
            self.assertEqual(retry_action["status"], "succeeded")
            with controller.store.read() as connection:
                replica = dict(
                    connection.execute(
                        "SELECT * FROM replicas WHERE replica_id = ?",
                        ("chat-alpha--0001",),
                    ).fetchone()
                )
            self.assertEqual(replica["failure_count"], 0)
            self.assertIsNone(replica["next_retry_tick"])

    def test_lifecycle_scale_down_reclaims_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            _,
            url,
        ):
            root = Path(temporary)
            services, nodes, trace = mini_inputs(
                root,
                target_tick0=2,
                target_tick1=0,
            )
            bundle = load_input_bundle(services, nodes, trace)
            controller = make_controller(root, url, bundle=bundle)
            launched = controller.run_round(round_id="up", tick=0)
            self.assertEqual(launched["replica_phases"]["tiny"], {"cold_starting": 2})
            drained = controller.run_round(round_id="down", tick=1)
            self.assertEqual(
                drained["services"][0]["state_before_round"]["ready_replicas"],
                2,
            )
            self.assertEqual(drained["outcome_counts"]["drain_succeeded"], 2)
            self.assertEqual(drained["replica_phases"]["tiny"], {"reclaimed": 2})
            self.assertEqual(drained["capacity_after"]["node_instances_used"]["node-0"], 0)
            self.assertEqual(
                drained["capacity_after"]["gpu_free_mib"]["node-0"],
                [4000],
            )

    def test_full_sample_is_byte_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server(
            json.loads((SAMPLES / "faults.json").read_text())
        ) as (_, url):
            root = Path(temporary)
            outputs: list[dict[str, str]] = []
            for attempt in range(2):
                from .support import reset_server

                reset_server(url)
                attempt_root = root / str(attempt)
                controller = make_controller(
                    attempt_root,
                    url,
                    bundle=sample_bundle(),
                )
                result_files: dict[str, str] = {}
                for window in sample_bundle().trace.windows:
                    round_id = f"repeat--t{window.tick:04d}"
                    result = controller.run_round(
                        round_id=round_id,
                        tick=window.tick,
                    )
                    result_files[round_id] = stable_dump(result)
                outputs.append(result_files)
            self.assertEqual(outputs[0], outputs[1])


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_same_round_across_processes_launches_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            server,
            url,
        ):
            root = Path(temporary)
            common = {
                "services": SAMPLES / "services.json",
                "nodes": SAMPLES / "nodes.json",
                "trace": SAMPLES / "trace.json",
                "db": root / "state.db",
                "runtime": root / "runtime",
                "url": url,
                "round_id": "same-round",
                "tick": 0,
                "service": "chat-alpha",
            }
            left = run_worker(
                script="round_worker.py",
                out=root / "left.json",
                **common,
            )
            right = run_worker(
                script="round_worker.py",
                out=root / "right.json",
                **common,
            )
            _, left_err = left.communicate(timeout=20)
            _, right_err = right.communicate(timeout=20)
            self.assertEqual(left.returncode, 0, left_err)
            self.assertEqual(right.returncode, 0, right_err)
            self.assertEqual(server_state(url)["request_count"], 1)
            self.assertEqual(
                json.loads((root / "left.json").read_text()),
                json.loads((root / "right.json").read_text()),
            )
            with sqlite3.connect(root / "state.db") as connection:
                action_count = connection.execute(
                    "SELECT COUNT(*) FROM actions"
                ).fetchone()[0]
                snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM snapshots"
                ).fetchone()[0]
            self.assertEqual(action_count, 1)
            self.assertEqual(snapshot_count, 1)
            self.assertEqual(server.fault_state.snapshot()["idempotency_hits"], 0)

    def test_forced_termination_after_launch_replays_without_double_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            _server,
            url,
        ):
            root = Path(temporary)
            services, nodes, trace = mini_inputs(
                root,
                target_tick0=1,
                target_tick1=0,
            )
            db_path = root / "state.db"
            runtime = root / "runtime"
            worker = run_worker(
                script="crash_worker.py",
                services=services,
                nodes=nodes,
                trace=trace,
                db=db_path,
                runtime=runtime,
                url=url,
                round_id="crash-launch",
                tick=0,
                service="tiny",
            )
            _, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 77, stderr)
            self.assertEqual(server_state(url)["request_count"], 1)

            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                action = dict(
                    connection.execute(
                        "SELECT * FROM actions WHERE round_id = 'crash-launch'"
                    ).fetchone()
                )
                round_row = dict(
                    connection.execute(
                        "SELECT * FROM rounds WHERE round_id = 'crash-launch'"
                    ).fetchone()
                )
            self.assertEqual(action["status"], "in_flight")
            self.assertEqual(round_row["budget_used_ticks"], 1)

            bundle = load_input_bundle(services, nodes, trace)
            store = Store(db_path, runtime)
            store.initialize()
            controller = Controller(
                bundle=bundle,
                policy=parse_policy(
                    {
                        "logical_time_budget_ticks": 8,
                        "action_timeout_seconds": 1.0,
                    }
                ),
                store=store,
                launcher=HTTPControlClient(url),
            )
            recovered = controller.run_round(
                round_id="crash-launch",
                tick=0,
                service_filter=["tiny"],
            )
            state = server_state(url)
            self.assertEqual(state["request_count"], 1)
            self.assertEqual(state["idempotency_hits"], 1)
            self.assertEqual(
                recovered["services"][0]["actions"][0]["status"],
                "succeeded",
            )
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                round_row = dict(
                    connection.execute(
                        "SELECT * FROM rounds WHERE round_id = 'crash-launch'"
                    ).fetchone()
                )
                occupancy_count = connection.execute(
                    "SELECT COUNT(*) FROM occupancies"
                ).fetchone()[0]
            self.assertEqual(round_row["budget_used_ticks"], 1)
            self.assertEqual(occupancy_count, 1)

    def test_forced_termination_after_drain_releases_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            _server,
            url,
        ):
            root = Path(temporary)
            services, nodes, trace = mini_inputs(
                root,
                target_tick0=1,
                target_tick1=0,
            )
            bundle = load_input_bundle(services, nodes, trace)
            db_path = root / "state.db"
            runtime = root / "runtime"
            controller = make_controller(root, url, bundle=bundle)
            controller.run_round(round_id="drain-up", tick=0, service_filter=["tiny"])
            up_requests = server_state(url)["request_count"]

            worker = run_worker(
                script="crash_worker.py",
                services=services,
                nodes=nodes,
                trace=trace,
                db=db_path,
                runtime=runtime,
                url=url,
                round_id="crash-drain",
                tick=1,
                service="tiny",
            )
            _, stderr = worker.communicate(timeout=10)
            self.assertEqual(worker.returncode, 77, stderr)
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                replica = dict(
                    connection.execute(
                        "SELECT * FROM replicas WHERE service_name = 'tiny'"
                    ).fetchone()
                )
                action = dict(
                    connection.execute(
                        "SELECT * FROM actions WHERE round_id = 'crash-drain'"
                    ).fetchone()
                )
            self.assertEqual(replica["phase"], "draining")
            self.assertEqual(action["status"], "in_flight")

            recovered = controller.run_round(
                round_id="crash-drain",
                tick=1,
                service_filter=["tiny"],
            )
            state = server_state(url)
            self.assertEqual(state["request_count"], up_requests + 1)
            self.assertEqual(state["idempotency_hits"], 1)
            self.assertEqual(recovered["replica_phases"]["tiny"], {"reclaimed": 1})
            with sqlite3.connect(db_path) as connection:
                occupancy_count = connection.execute(
                    "SELECT COUNT(*) FROM occupancies"
                ).fetchone()[0]
            self.assertEqual(occupancy_count, 0)

    def test_parallel_services_do_not_oversell_nodes_or_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, running_server() as (
            server,
            url,
        ):
            root = Path(temporary)
            db_path = root / "state.db"
            runtime = root / "runtime"
            common = {
                "services": SAMPLES / "services.json",
                "nodes": SAMPLES / "nodes.json",
                "trace": SAMPLES / "trace.json",
                "db": db_path,
                "runtime": runtime,
                "url": url,
                "tick": 6,
            }
            left = run_worker(
                script="round_worker.py",
                round_id="parallel-chat",
                service="chat-alpha",
                out=root / "chat.json",
                **common,
            )
            right = run_worker(
                script="round_worker.py",
                round_id="parallel-embed",
                service="embed-beta",
                out=root / "embed.json",
                **common,
            )
            _, left_err = left.communicate(timeout=20)
            _, right_err = right.communicate(timeout=20)
            self.assertEqual(left.returncode, 0, left_err)
            self.assertEqual(right.returncode, 0, right_err)

            bundle = sample_bundle()
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                occupancies = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM occupancies ORDER BY node_name, gpu_index"
                    ).fetchall()
                ]
                snapshots = connection.execute(
                    "SELECT COUNT(*) FROM snapshots"
                ).fetchone()[0]
                action_keys = [
                    row["idempotency_key"]
                    for row in connection.execute(
                        "SELECT idempotency_key FROM actions ORDER BY action_id"
                    ).fetchall()
                ]
                commits = [
                    json.loads(row["result_json"])
                    for row in connection.execute(
                        "SELECT result_json FROM rounds WHERE status = 'committed'"
                    ).fetchall()
                ]
            self.assertEqual(snapshots, 2)
            self.assertEqual(len(commits), 2)
            observed = [item["snapshot"]["version_observed"] for item in commits]
            if observed[0] == observed[1]:
                self.assertEqual(
                    sum(1 for item in commits if item["snapshot"]["rebased"]),
                    1,
                )
            node_map = bundle.node_pool.node_map()
            for (node_name, gpu_index), items in self._group(
                occupancies,
                lambda item: (item["node_name"], item["gpu_index"]),
            ).items():
                used = sum(item["memory_mib"] for item in items)
                self.assertLessEqual(
                    used,
                    node_map[node_name].memory_mib_per_gpu,
                )
            for node_name, items in self._group(
                occupancies,
                lambda item: item["node_name"],
            ).items():
                self.assertLessEqual(
                    len(items),
                    node_map[node_name].max_instances_per_node,
                )
            self.assertEqual(len(action_keys), len(set(action_keys)))
            self.assertEqual(server.fault_state.snapshot()["request_count"], len(occupancies))

    @staticmethod
    def _group(items, key):
        output = {}
        for item in items:
            output.setdefault(key(item), []).append(item)
        return output


if __name__ == "__main__":
    unittest.main()

