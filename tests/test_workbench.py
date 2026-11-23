from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tunnel_workbench.api import create_app
from tunnel_workbench.cli import main as cli_main
from tunnel_workbench.config import (
    LinkProfile,
    load_config,
    parse_config,
    parse_profile_update,
)
from tunnel_workbench.diagnostics import verify_state
from tunnel_workbench.echo_service import EchoService
from tunnel_workbench.engine import ForwardEngine
from tunnel_workbench.errors import BusinessError
from tunnel_workbench.framing import (
    Reassembler,
    build_fragment,
    parse_fragment,
    stream_token,
)
from tunnel_workbench.payload import generate_payload
from tunnel_workbench.storage import Storage
from tunnel_workbench.util import sha256_hex, stable_json

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sample_document(
    *,
    alpha_port: int | None = None,
    beta_port: int | None = None,
    budget: int = 62,
    concurrency: int = 2,
    retransmission_budget: int = 1,
    alpha_loss: float = 0.0,
    alpha_rtt: float = 20.0,
    alpha_jitter: float = 2.0,
    include_beta: bool = True,
) -> dict:
    alpha_port = alpha_port or free_port()
    beta_port = beta_port or free_port()
    tunnels = [
        {
            "name": "alpha",
            "listen": f"127.0.0.1:{alpha_port}",
            "transport": "udp",
            "outer_mtu": 220,
            "encapsulation_overhead": 8,
            "link_weight": 2.0,
            "payload_lengths": [64, 96],
        }
    ]
    profiles = [
        {
            "tunnel": "alpha",
            "version": 1,
            "rtt_ms": alpha_rtt,
            "loss_rate": alpha_loss,
            "bandwidth_kbps": 4000,
            "jitter_ms": alpha_jitter,
        }
    ]
    if include_beta:
        tunnels.append(
            {
                "name": "beta",
                "listen": f"127.0.0.1:{beta_port}",
                "transport": "tcp",
                "outer_mtu": 180,
                "encapsulation_overhead": 12,
                "link_weight": 1.0,
                "payload_lengths": [45, 75],
            }
        )
        profiles.append(
            {
                "tunnel": "beta",
                "version": 1,
                "rtt_ms": 30,
                "loss_rate": 0.0,
                "bandwidth_kbps": 2500,
                "jitter_ms": 3,
            }
        )
    return {
        "tunnels": tunnels,
        "profiles": profiles,
        "policy": {
            "fragment_threshold": 40,
            "per_round_concurrency": concurrency,
            "logical_budget_ms": budget,
            "retransmission_budget": retransmission_budget,
            "tolerance": 0,
            "clock_step_ms": 5,
        },
    }


@contextlib.contextmanager
def workbench(document: dict):
    config = parse_config(document)
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "workbench.db"
        storage = Storage(db_path)
        storage.install_config(config)
        with EchoService.from_config(config):
            yield config, storage, db_path


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


class ValidationAndFramingTests(unittest.TestCase):
    def test_invalid_configuration_does_not_create_database(self) -> None:
        document = sample_document(include_beta=False)
        document["tunnels"].append(copy.deepcopy(document["tunnels"][0]))
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "invalid.json"
            db_path = Path(directory) / "state.db"
            write_json(config_path, document)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli_main(
                    ["load", "--config", str(config_path), "--db", str(db_path)]
                )
            self.assertEqual(code, 2)
            self.assertFalse(db_path.exists())
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["error"]["code"], "E_NAME_DUPLICATE")

    def test_validation_business_codes(self) -> None:
        cases = []
        document = sample_document(include_beta=False)
        document["tunnels"][0]["listen"] = "192.0.2.10:9000"
        cases.append((document, "E_LISTEN_NOT_LOOPBACK"))

        document = sample_document(include_beta=False)
        document["tunnels"][0]["payload_lengths"] = [133]
        cases.append((document, "E_PAYLOAD_TOO_LARGE"))

        document = sample_document(include_beta=False)
        document["profiles"][0]["loss_rate"] = 1.1
        cases.append((document, "E_LOSS_RATE_INVALID"))

        document = sample_document(include_beta=False)
        document["profiles"][0]["jitter_ms"] = 21
        cases.append((document, "E_JITTER_INVALID"))

        document = sample_document(include_beta=False)
        document["policy"]["per_round_concurrency"] = 0
        cases.append((document, "E_CONCURRENCY_INVALID"))

        document = sample_document(include_beta=False)
        document["policy"]["fragment_threshold"] = 0
        cases.append((document, "E_FRAGMENT_THRESHOLD_INVALID"))

        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(BusinessError) as raised:
                    parse_config(value)
                self.assertEqual(raised.exception.code, code)

    def test_deterministic_fragmentation_and_distinct_reassembly_errors(self) -> None:
        payload = generate_payload("alpha", 96, 7)
        digest = sha256_hex(payload)
        stream = stream_token("alpha", digest, 220, 40, 8, 1, "run-fixed")
        frames = [
            build_fragment(
                payload,
                offset=offset,
                length=length,
                sequence=sequence,
                tunnel="alpha",
                stream_token_value=stream,
                total_digest=bytes.fromhex(digest),
                overhead=8,
                profile_version=1,
                outer_mtu=220,
            ).frame
            for sequence, (offset, length) in enumerate([(0, 40), (40, 40), (80, 16)])
        ]
        again = [
            build_fragment(
                payload,
                offset=offset,
                length=length,
                sequence=sequence,
                tunnel="alpha",
                stream_token_value=stream,
                total_digest=bytes.fromhex(digest),
                overhead=8,
                profile_version=1,
                outer_mtu=220,
            )
            for sequence, (offset, length) in enumerate([(0, 40), (40, 40), (80, 16)])
        ]
        self.assertEqual(frames, [fragment.frame for fragment in again])
        self.assertEqual(
            [parse_fragment(frame).fragment_digest for frame in frames],
            [fragment.fragment_digest for fragment in again],
        )
        reassembler = Reassembler()
        for frame in frames:
            reassembler.add(frame)
        summary = reassembler.finalize()
        self.assertEqual(summary.payload, payload)
        self.assertEqual(summary.digest, digest)

        with self.assertRaises(BusinessError) as duplicate:
            reassembler.add(frames[0])
        self.assertEqual(duplicate.exception.code, "E_DUPLICATE_FRAGMENT")

        missing = Reassembler()
        missing.add(frames[0])
        missing.add(frames[2])
        with self.assertRaises(BusinessError) as absent:
            missing.finalize()
        self.assertEqual(absent.exception.code, "E_MISSING_FRAGMENT")
        self.assertEqual(absent.exception.details["missing"], [1])

        corrupt = Reassembler()
        corrupt.add(frames[0])
        corrupt.add(frames[1])
        changed = bytearray(frames[2])
        changed[-1] ^= 0xFF
        corrupt.add(bytes(changed))
        with self.assertRaises(BusinessError) as digest_error:
            corrupt.finalize()
        self.assertEqual(digest_error.exception.code, "E_DIGEST_MISMATCH")

    def test_profile_version_is_part_of_header_identity(self) -> None:
        payload = generate_payload("alpha", 20, 0)
        digest = sha256_hex(payload)
        stream = stream_token("alpha", digest, 220, 40, 8, 1, "run")
        first = build_fragment(
            payload,
            offset=0,
            length=20,
            sequence=0,
            tunnel="alpha",
            stream_token_value=stream,
            total_digest=bytes.fromhex(digest),
            overhead=8,
            profile_version=1,
            outer_mtu=220,
        )
        second = build_fragment(
            payload,
            offset=0,
            length=20,
            sequence=0,
            tunnel="alpha",
            stream_token_value=stream,
            total_digest=bytes.fromhex(digest),
            overhead=8,
            profile_version=2,
            outer_mtu=220,
        )
        self.assertNotEqual(first.frame, second.frame)
        self.assertEqual(second.header.profile_version, 2)


class ForwardingTests(unittest.TestCase):
    def test_round_progress_resume_and_stable_status(self) -> None:
        document = sample_document()
        with workbench(document) as (config, storage, _):
            engine = ForwardEngine(storage, config)
            first = engine.run_new(tunnel="alpha", payload_index=1, rounds=1)
            self.assertEqual(first["run"]["status"], "PARTIAL")
            self.assertEqual(first["run"]["committed_watermark"], 2)
            self.assertEqual(first["run"]["logical_clock_ms"], 50)
            self.assertEqual(first["rounds"][0]["started_clock_ms"], 0)
            self.assertEqual(first["rounds"][0]["deadline_clock_ms"], 62)

            second = engine.resume(tunnel="alpha", rounds=1)
            self.assertEqual(second["run"]["status"], "COMPLETED")
            self.assertEqual(second["run"]["logical_clock_ms"], 75)
            self.assertEqual(second["run"]["reassembly_digest"], second["run"]["payload_digest"])
            fragments = [
                parse_fragment(fragment["wire"])
                for fragment in storage.fragments_for_run(second["run"]["run_id"])
            ]
            self.assertEqual([fragment.header.payload_length for fragment in fragments], [40, 40, 16])
            self.assertTrue(fragments[-1].header.is_last)
            self.assertEqual(verify_state(storage, config)["anomalies"], [])

            status_one = stable_json(storage.status())
            status_two = stable_json(storage.status())
            self.assertEqual(status_one, status_two)
            status = json.loads(status_one)
            self.assertEqual([tunnel["tunnel"] for tunnel in status["tunnels"]], ["alpha", "beta"])

    def test_retransmission_exhaustion_is_deterministic(self) -> None:
        document = sample_document(
            include_beta=False,
            budget=40,
            concurrency=1,
            retransmission_budget=2,
            alpha_loss=1.0,
            alpha_rtt=10,
            alpha_jitter=1,
        )
        with workbench(document) as (config, storage, _):
            engine = ForwardEngine(storage, config)
            result = engine.run_new(tunnel="alpha", payload_index=1, rounds=0)
            self.assertEqual(result["run"]["status"], "FAILED")
            self.assertEqual(result["final_outcome"], "FAILED_AFTER_SEND")
            self.assertEqual(result["business_code"], "E_RETRANSMIT_EXHAUSTED")
            self.assertEqual(result["run"]["retransmission_count"], 2)
            self.assertEqual(result["diagnostic"]["retransmission_generation"], 2)
            self.assertEqual(result["diagnostic"]["trigger_source"], "PROFILE_LOSS")
            first_wires = [
                fragment["wire"].hex() for fragment in storage.fragments_for_run(result["run"]["run_id"])
            ]
        with workbench(copy.deepcopy(document)) as (config, storage, _):
            second = ForwardEngine(storage, config).run_new(
                tunnel="alpha", payload_index=1, rounds=0
            )
            second_wires = [
                fragment["wire"].hex() for fragment in storage.fragments_for_run(second["run"]["run_id"])
            ]
        self.assertEqual(first_wires, second_wires)

    def test_profile_adaptation_preserves_unconfirmed_wire(self) -> None:
        class StopAfterIntent(Exception):
            pass

        document = sample_document(include_beta=False)
        with workbench(document) as (config, storage, _):
            def stop(event, payload):
                del payload
                if event == "AFTER_INTENT":
                    raise StopAfterIntent()

            with self.assertRaises(StopAfterIntent):
                ForwardEngine(storage, config, kill_hook=stop).run_new(
                    tunnel="alpha", payload_index=1, rounds=1
                )
            before = storage.fragments_for_run(
                storage.latest_resumable_run("alpha")["run_id"]
            )
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0]["status"], "INFLIGHT")
            original_wire = before[0]["wire"]
            original_length = before[0]["length"]

            profile = parse_profile_update(
                {
                    "version": 2,
                    "rtt_ms": 90,
                    "loss_rate": 0.0,
                    "bandwidth_kbps": 1000,
                    "jitter_ms": 5,
                },
                "alpha",
            )
            replaced = storage.replace_profile(profile)
            self.assertEqual(replaced["profile_version"], 2)
            with self.assertRaises(BusinessError) as rollback:
                storage.replace_profile(
                    LinkProfile("alpha", 1, 20.0, 0.0, 4000.0, 2.0)
                )
            self.assertEqual(rollback.exception.code, "E_PROFILE_ROLLBACK")

            resumed = ForwardEngine(storage, config).resume(tunnel="alpha", rounds=6)
            self.assertEqual(resumed["run"]["status"], "COMPLETED")
            self.assertLess(resumed["run"]["current_mtu"], resumed["run"]["initial_mtu"])
            fragments = storage.fragments_for_run(resumed["run"]["run_id"])
            self.assertEqual(fragments[0]["wire"], original_wire)
            self.assertEqual(fragments[0]["length"], original_length)
            self.assertEqual(fragments[0]["profile_version"], 1)
            self.assertEqual(fragments[0]["profile_version"], 1)
            self.assertTrue(
                all(fragment["profile_version"] == 2 for fragment in fragments[1:])
            )
            self.assertEqual(verify_state(storage, config)["anomalies"], [])

    def test_duplicate_confirmation_is_idempotent(self) -> None:
        document = sample_document(include_beta=False)
        with workbench(document) as (config, storage, _):
            result = ForwardEngine(storage, config).run_new(
                tunnel="alpha", payload_index=0, rounds=0
            )
            fragment = storage.fragments_for_run(result["run"]["run_id"])[0]
            duplicate = storage.confirm_fragment(
                run_id=result["run"]["run_id"],
                seq=fragment["seq"],
                ack_clock_ms=999,
                fragment_digest=fragment["fragment_digest"],
                receiver_summary=None,
            )
            self.assertTrue(duplicate["idempotent"])
            with sqlite3.connect(storage.db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM confirmations WHERE run_id=? AND seq=?",
                    (result["run"]["run_id"], fragment["seq"]),
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_active_abort_is_a_distinct_outcome(self) -> None:
        document = sample_document(
            include_beta=False,
            budget=60,
            concurrency=1,
            retransmission_budget=1,
            alpha_loss=1.0,
            alpha_rtt=100,
            alpha_jitter=5,
        )
        with workbench(document) as (config, storage, _):
            result_holder: dict[str, dict] = {}

            def run() -> None:
                result_holder["result"] = ForwardEngine(storage, config).run_new(
                    tunnel="alpha", payload_index=0, rounds=1
                )

            thread = threading.Thread(target=run)
            thread.start()
            run = None
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    run = storage.latest_resumable_run("alpha")
                    if storage.fragments_for_run(run["run_id"]):
                        break
                except BusinessError:
                    pass
                time.sleep(0.005)
            self.assertIsNotNone(run)
            storage.request_abort(run["run_id"], 10)
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            result = result_holder["result"]
            self.assertEqual(result["run"]["status"], "ABORTED")
            self.assertEqual(result["final_outcome"], "ABORTED_ACTIVE")
            self.assertEqual(result["business_code"], "E_ACTIVE_ABORT")
            self.assertEqual(result["run"]["committed_watermark"], 0)

    def test_profile_update_does_not_change_unaffected_tunnel_wires(self) -> None:
        first_document = sample_document()
        with workbench(first_document) as (first_config, first_storage, _):
            first = ForwardEngine(first_storage, first_config).run_new(
                tunnel="beta", payload_index=1, rounds=0
            )
            first_wires = [
                fragment["wire"].hex()
                for fragment in first_storage.fragments_for_run(first["run"]["run_id"])
            ]

        second_document = sample_document()
        with workbench(second_document) as (second_config, second_storage, _):
            second_storage.replace_profile(
                parse_profile_update(
                    {
                        "version": 2,
                        "rtt_ms": 90,
                        "loss_rate": 0.4,
                        "bandwidth_kbps": 1000,
                        "jitter_ms": 5,
                    },
                    "alpha",
                )
            )
            second = ForwardEngine(second_storage, second_config).run_new(
                tunnel="beta", payload_index=1, rounds=0
            )
            second_wires = [
                fragment["wire"].hex()
                for fragment in second_storage.fragments_for_run(second["run"]["run_id"])
            ]
        self.assertEqual(first_wires, second_wires)

    def test_verify_reports_recomputation_anomalies(self) -> None:
        document = sample_document(include_beta=False)
        with workbench(document) as (config, storage, _):
            result = ForwardEngine(storage, config).run_new(
                tunnel="alpha", payload_index=1, rounds=0
            )
            with sqlite3.connect(storage.db_path) as conn:
                conn.execute(
                    "UPDATE fragments SET length=length+1 WHERE run_id=? AND seq=1",
                    (result["run"]["run_id"],),
                )
            report = verify_state(storage, config)
            self.assertFalse(report["ok"])
            codes = {anomaly["code"] for anomaly in report["anomalies"]}
            self.assertIn("E_VERIFY_OFFSET", codes)
            self.assertIn("E_VERIFY_NEXT_OFFSET", codes)


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_hard_kill_replays_only_unconfirmed_fragments(self) -> None:
        document = sample_document(include_beta=False)
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "config.json"
            db_path = directory_path / "workbench.db"
            write_json(config_path, document)
            config = load_config(config_path)
            storage = Storage(db_path)
            storage.install_config(config)
            with EchoService.from_config(config):
                environment = os.environ.copy()
                environment["TWB_TEST_KILL_POINT"] = "AFTER_ACK_BEFORE_COMMIT"
                environment["TWB_TEST_KILL_COUNT"] = "2"
                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tunnel_workbench",
                        "forward",
                        "--config",
                        str(config_path),
                        "--db",
                        str(db_path),
                        "--tunnel",
                        "alpha",
                        "--payload-index",
                        "1",
                        "--rounds",
                        "0",
                    ],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(process.returncode, 137)
                self.assertEqual(process.stdout, "")
                run = storage.latest_resumable_run("alpha")
                fragments = storage.fragments_for_run(run["run_id"])
                self.assertEqual(fragments[0]["status"], "COMMITTED")
                self.assertEqual(fragments[1]["status"], "INFLIGHT")
                self.assertEqual(run["committed_watermark"], 1)
                committed_wire = fragments[0]["wire"]

                resumed = ForwardEngine(storage, config).resume(tunnel="alpha", rounds=6)
                self.assertEqual(resumed["run"]["status"], "COMPLETED")
                final_fragments = storage.fragments_for_run(run["run_id"])
                self.assertEqual(final_fragments[0]["wire"], committed_wire)
                self.assertEqual(verify_state(storage, config)["anomalies"], [])
                duplicates = sum(
                    round_result["duplicate_echo_acks"] for round_result in resumed["rounds"]
                )
                self.assertGreaterEqual(duplicates, 1)

    def test_different_tunnels_forward_in_parallel(self) -> None:
        document = sample_document()
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "config.json"
            db_path = directory_path / "workbench.db"
            write_json(config_path, document)
            config = load_config(config_path)
            storage = Storage(db_path)
            storage.install_config(config)
            with EchoService.from_config(config):
                commands = []
                for tunnel, payload_index in (("alpha", 1), ("beta", 1)):
                    commands.append(
                        [
                            sys.executable,
                            "-m",
                            "tunnel_workbench",
                            "forward",
                            "--config",
                            str(config_path),
                            "--db",
                            str(db_path),
                            "--tunnel",
                            tunnel,
                            "--payload-index",
                            str(payload_index),
                            "--rounds",
                            "0",
                        ]
                    )
                processes = [
                    subprocess.Popen(
                        command,
                        cwd=ROOT,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for command in commands
                ]
                outputs = [process.communicate(timeout=20) for process in processes]
                for process, (stdout, stderr) in zip(processes, outputs):
                    self.assertEqual(process.returncode, 0, msg=stderr)
                    self.assertTrue(json.loads(stdout)["ok"])
                runs = storage.all_runs()
                self.assertEqual({run["tunnel"] for run in runs}, {"alpha", "beta"})
                self.assertTrue(all(run["status"] == "COMPLETED" for run in runs))
                self.assertEqual(verify_state(storage, config)["anomalies"], [])

    def test_same_tunnel_cross_process_lock(self) -> None:
        document = sample_document(include_beta=False)
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "config.json"
            db_path = directory_path / "workbench.db"
            write_json(config_path, document)
            config = load_config(config_path)
            storage = Storage(db_path)
            storage.install_config(config)
            lock_dir = db_path.parent / "locks"
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import time; "
                        "from tunnel_workbench.locks import TunnelFileLock; "
                        f"lock=TunnelFileLock({str(lock_dir)!r}, 'alpha'); "
                        "lock.acquire(); print('LOCKED', flush=True); "
                        "time.sleep(10)"
                    ),
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                line = holder.stdout.readline()
                self.assertEqual(line.strip(), "LOCKED")
                process = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tunnel_workbench",
                        "forward",
                        "--config",
                        str(config_path),
                        "--db",
                        str(db_path),
                        "--tunnel",
                        "alpha",
                        "--rounds",
                        "1",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(process.returncode, 2)
                self.assertEqual(json.loads(process.stdout)["error"]["code"], "E_TUNNEL_LOCKED")
            finally:
                holder.terminate()
                holder.wait(timeout=5)
                holder.stdout.close()
                holder.stderr.close()


class ApiTests(unittest.TestCase):
    def test_http_load_forward_status_verify_and_profile_rollback(self) -> None:
        document = sample_document(include_beta=False)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "api.db"
            config = parse_config(document)
            storage = Storage(db_path)
            storage.install_config(config)
            app = create_app(db_path=db_path, config=config)
            client = app.test_client()
            with EchoService.from_config(config):
                dashboard = client.get("/")
                self.assertEqual(dashboard.status_code, 200)
                self.assertIn(b"Tunnel Workbench", dashboard.data)
                loaded = client.post("/api/load", json={"config": document})
                self.assertEqual(loaded.status_code, 200)
                self.assertTrue(loaded.get_json()["ok"])
                forwarded = client.post(
                    "/api/forward",
                    json={"tunnel": "alpha", "payload_index": 1, "rounds": 0},
                )
                self.assertEqual(forwarded.status_code, 200)
                self.assertEqual(forwarded.get_json()["run"]["status"], "COMPLETED")
                status = client.get("/api/status").get_json()
                self.assertEqual(status["tunnels"][0]["fragment_watermark"], 3)
                verified = client.get("/api/verify").get_json()
                self.assertTrue(verified["ok"])
                self.assertEqual(verified["anomalies"], [])

                version_two = {
                    "version": 2,
                    "rtt_ms": 40,
                    "loss_rate": 0.1,
                    "bandwidth_kbps": 1000,
                    "jitter_ms": 3,
                }
                replaced = client.post("/api/profiles/alpha", json=version_two)
                self.assertEqual(replaced.status_code, 200)
                rollback = client.post(
                    "/api/profiles/alpha",
                    json={
                        "version": 1,
                        "rtt_ms": 20,
                        "loss_rate": 0.0,
                        "bandwidth_kbps": 4000,
                        "jitter_ms": 2,
                    },
                )
                self.assertEqual(rollback.status_code, 409)
                self.assertEqual(
                    rollback.get_json()["error"]["code"], "E_PROFILE_ROLLBACK"
                )


if __name__ == "__main__":
    unittest.main()

