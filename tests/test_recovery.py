from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, fast_scenario

from scaler.models import canonical_json
from scaler.storage import SQLiteStore
from scaler.stub import StubServer


def _write_config(base_spec, path: Path, url: str) -> None:
    raw = base_spec.to_dict()
    raw["config"]["launch_target_url"] = url
    path.write_text(json.dumps(raw["config"], indent=2), encoding="utf-8")


def _cli(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "scaler", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_forced_termination_recovery_matches_uninterrupted_run(
    tmp_path: Path, base_spec
) -> None:
    scenario = fast_scenario()
    with StubServer(port=0, scenario=scenario) as stub:
        config_path = tmp_path / "config.json"
        _write_config(base_spec, config_path, stub.url)
        common = [
            "--services", str(ROOT / "samples/services.json"),
            "--nodes", str(ROOT / "samples/nodes.json"),
            "--trace", str(ROOT / "samples/trace.json"),
            "--config", str(config_path),
        ]
        baseline = _cli(["run", "--db", str(tmp_path / "baseline.db"), *common, "--rounds", "2"])
        assert baseline.returncode == 0, baseline.stderr

        stub.state.operations.clear()
        stub.state.service_attempts.clear()
        crash = _cli(
            [
                "run",
                "--db",
                str(tmp_path / "recovered.db"),
                *common,
                "--rounds",
                "2",
                "--fault-round",
                "2",
                "--fault-after-dispatch",
                "2",
            ]
        )
        assert crash.returncode in {-9, 137}, (crash.returncode, crash.stderr)

        crash_store = SQLiteStore(tmp_path / "recovered.db")
        pending_state = crash_store.load()[0]
        assert pending_state["pending_rounds"]
        pending_budget = next(iter(pending_state["pending_rounds"].values()))["budget"]

        recovered = _cli(["recover", "--db", str(tmp_path / "recovered.db")])
        assert recovered.returncode == 0, recovered.stderr
        recovered_output = json.loads(recovered.stdout)
        baseline_state = SQLiteStore(tmp_path / "baseline.db").load()[0]
        recovered_state = crash_store.load()[0]
        assert canonical_json(recovered_state) == canonical_json(baseline_state)
        assert not recovered_state["pending_rounds"]
        assert recovered_state["state_revision"] == recovered_state["round_watermark"] == 2

        recovered_round = recovered_output["recovered_rounds"][0]["round"]
        assert recovered_round["budget"] == pending_budget
        assert recovered_round["budget"]["reserved_ms"] <= recovered_round["budget"]["total_ms"]
        assert crash_store.read_recovery_audit()
        events = crash_store.read_events()
        assert any(event["kind"] == "recovery.applied" for event in events)


def test_multiprocess_service_runs_do_not_oversell(
    tmp_path: Path, base_spec
) -> None:
    scenario = fast_scenario()
    with StubServer(port=0, scenario=scenario) as stub:
        config_path = tmp_path / "config.json"
        _write_config(base_spec, config_path, stub.url)
        common = [
            "--services", str(ROOT / "samples/services.json"),
            "--nodes", str(ROOT / "samples/nodes.json"),
            "--trace", str(ROOT / "samples/trace.json"),
            "--config", str(config_path),
            "--rounds", "1",
            "--start-clock-ms", "1000",
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        db = str(tmp_path / "concurrent.db")
        commands = [
            [sys.executable, "-m", "scaler", "run", "--db", db, *common, "--service", "chat-alpha"],
            [sys.executable, "-m", "scaler", "run", "--db", db, *common, "--service", "embed-beta"],
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for command in commands
        ]
        outputs = [process.communicate(timeout=30) for process in processes]
        for process, (stdout, stderr) in zip(processes, outputs):
            assert process.returncode == 0, (stdout, stderr)

        store = SQLiteStore(tmp_path / "concurrent.db")
        state = store.load()[0]
        from scaler.state import validate_state

        validate_state(state)
        operation_ids = [
            replica["launch_operation_id"] for replica in state["replicas"].values()
        ]
        assert len(operation_ids) == len(set(operation_ids))
        assert not state["pending_rounds"]
        for node in state["nodes"].values():
            assert all(value >= 0 for value in node["gpu_remaining_mib"])
        assert len(state["round_history"]) == 2


def test_same_service_duplicate_controllers_launch_once(
    tmp_path: Path, base_spec
) -> None:
    scenario = fast_scenario()
    with StubServer(port=0, scenario=scenario) as stub:
        config_path = tmp_path / "config.json"
        _write_config(base_spec, config_path, stub.url)
        common = [
            "--services", str(ROOT / "samples/services.json"),
            "--nodes", str(ROOT / "samples/nodes.json"),
            "--trace", str(ROOT / "samples/trace.json"),
            "--config", str(config_path),
            "--rounds", "1",
            "--start-clock-ms", "1000",
            "--service", "chat-alpha",
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        db = str(tmp_path / "same-service.db")
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "scaler", "run", "--db", db, *common],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
        state = SQLiteStore(tmp_path / "same-service.db").load()[0]
        alpha = [
            replica
            for replica in state["replicas"].values()
            if replica["service"] == "chat-alpha"
        ]
        assert len(alpha) == 2
        assert len({item["launch_operation_id"] for item in alpha}) == 2
        assert all(item["phase"] == "READY" for item in alpha)
