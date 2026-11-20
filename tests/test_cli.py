from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tunnel_kernel.echo_service import LoopbackEchoService

from conftest import ROOT, cli_config_args, free_port, write_cli_configs


def _run_cli(args: list[str], *, env_updates: dict[str, str] | None = None):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    if env_updates:
        environment.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "tunnel_kernel.cli", *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )


def _start_service() -> tuple[LoopbackEchoService, dict[str, int]]:
    service = LoopbackEchoService(udp_port=free_port(), tcp_port=free_port())
    return service, service.start()


def test_invalid_load_returns_code_and_writes_no_database(tmp_path) -> None:
    service, ports = _start_service()
    try:
        paths = write_cli_configs(tmp_path, ports=ports)
        peers = json.loads(paths["peers"].read_text(encoding="utf-8"))
        peers["peers"][0]["outer_mtu"] = 80
        paths["peers"].write_text(json.dumps(peers), encoding="utf-8")
        db = tmp_path / "must-not-exist.sqlite3"
        completed = _run_cli(["load", *cli_config_args(paths, db)])
        assert completed.returncode == 2
        error = json.loads(completed.stderr)
        assert error["code"] == "E_BAD_MTU"
        assert not db.exists()
    finally:
        service.stop()


def test_cli_end_to_end_status_verify_and_stored_trace_replay(tmp_path) -> None:
    service, ports = _start_service()
    try:
        paths = write_cli_configs(tmp_path, ports=ports)
        db = tmp_path / "cli.sqlite3"
        config = cli_config_args(paths, db)

        loaded = _run_cli(["load", *config])
        assert loaded.returncode == 0, loaded.stderr
        assert json.loads(loaded.stdout)["code"] == "OK"
        assert _run_cli(["load", *config]).stdout == loaded.stdout

        prepared = _run_cli(
            [
                "tunnel",
                *config,
                "--peer",
                "udp-alpha",
                "--payload-size",
                "1024",
                "--payload-seed",
                "7",
            ]
        )
        assert prepared.returncode == 0, prepared.stderr
        prepared_doc = json.loads(prepared.stdout)
        tunnel_id = prepared_doc["tunnel_id"]
        assert prepared_doc["algorithm"]["algorithm"] == "aggregate"
        assert prepared_doc["state"]["status"] == "prepared"

        forwarded = _run_cli(
            [
                "forward",
                *config,
                "--tunnel",
                tunnel_id,
                "--scenario",
                "degrade-udp",
            ]
        )
        assert forwarded.returncode == 0, forwarded.stderr
        forward_doc = json.loads(forwarded.stdout)
        assert forward_doc["status"] == "complete"
        assert forward_doc["reassembly_digest"] == prepared_doc["payload_digest"]

        replayed = _run_cli(
            ["forward", *config, "--tunnel", tunnel_id, "--scenario", "degrade-udp"]
        )
        assert replayed.returncode == 0, replayed.stderr
        assert replayed.stdout == forwarded.stdout

        first_status = _run_cli(["status", *config])
        second_status = _run_cli(["status", *config])
        assert first_status.returncode == 0
        assert first_status.stdout == second_status.stdout
        status_doc = json.loads(first_status.stdout)
        tunnel_status = status_doc["tunnels"][0]
        assert tunnel_status["current_mtu"] == 500
        assert tunnel_status["confirmed_through"] == 2
        assert tunnel_status["inflight_ranges"] == []
        assert tunnel_status["algorithm"] == "aggregate"

        verified = _run_cli(["verify", *config, "--tunnel", tunnel_id])
        assert verified.returncode == 0, verified.stderr
        verify_doc = json.loads(verified.stdout)
        assert verify_doc["code"] == "OK"
        assert verify_doc["checks"]["echo_digest_matches"] is True
    finally:
        service.stop()


def test_cli_forced_termination_then_resume_retransmits_unconfirmed(tmp_path) -> None:
    service, ports = _start_service()
    try:
        paths = write_cli_configs(tmp_path, ports=ports)
        db = tmp_path / "crash.sqlite3"
        config = cli_config_args(paths, db)
        prepared = _run_cli(
            [
                "tunnel",
                *config,
                "--peer",
                "udp-alpha",
                "--payload-size",
                "640",
                "--payload-seed",
                "9",
            ]
        )
        assert prepared.returncode == 0, prepared.stderr
        tunnel_id = json.loads(prepared.stdout)["tunnel_id"]

        crashed = _run_cli(
            ["forward", *config, "--tunnel", tunnel_id],
            env_updates={"TUNNEL_KERNEL_CRASH_AFTER_SENDS": "1"},
        )
        assert crashed.returncode == 137
        after_crash = _run_cli(["status", *config, "--tunnel", tunnel_id])
        crash_state = json.loads(after_crash.stdout)["tunnel"]
        assert crash_state["status"] == "in_progress"
        assert crash_state["confirmed_through"] == -1
        assert crash_state["inflight_ranges"] == [[0, 1]]

        resumed = _run_cli(["resume", *config, "--tunnel", tunnel_id])
        assert resumed.returncode == 0, resumed.stderr
        resumed_doc = json.loads(resumed.stdout)
        assert resumed_doc["status"] == "complete"
        assert resumed_doc["recovery_count"] == 1
        assert resumed_doc["retransmit_count"] == 2
        assert resumed_doc["confirmed_bytes"] == 640

        verified = _run_cli(["verify", *config, "--tunnel", tunnel_id])
        assert verified.returncode == 0, verified.stderr
    finally:
        service.stop()


def test_two_fresh_cli_runs_are_byte_identical(tmp_path) -> None:
    outputs: list[str] = []
    for run in range(2):
        service, ports = _start_service()
        try:
            run_dir = tmp_path / f"run-{run}"
            run_dir.mkdir()
            paths = write_cli_configs(run_dir, ports=ports)
            db = run_dir / "state.sqlite3"
            config = cli_config_args(paths, db)
            prepared = _run_cli(
                [
                    "tunnel",
                    *config,
                    "--peer",
                    "udp-alpha",
                    "--payload-size",
                    "1024",
                    "--payload-seed",
                    "7",
                ]
            )
            assert prepared.returncode == 0, prepared.stderr
            tunnel_id = json.loads(prepared.stdout)["tunnel_id"]
            forwarded = _run_cli(
                [
                    "forward",
                    *config,
                    "--tunnel",
                    tunnel_id,
                    "--scenario",
                    "degrade-udp",
                ]
            )
            assert forwarded.returncode == 0, forwarded.stderr
            outputs.append(forwarded.stdout)
        finally:
            service.stop()
    assert outputs[0] == outputs[1]
