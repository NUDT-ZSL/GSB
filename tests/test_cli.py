"""CLI-level tests: business codes via subprocess."""

import json
import subprocess
import sys


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "ddp_sim", *args],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def test_run_step_emits_stable_summary(base_port, tmp_path):
    log_dir = str(tmp_path / "run1")
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                   "--block-size", "16", "--base-port", str(base_port),
                   "--log-dir", log_dir)
    assert proc.returncode == 0, proc.stderr
    line = json.loads(proc.stdout.strip().splitlines()[-1])
    assert line["event"] == "step" and line["version"] == 1
    # determinism: identical invocation -> identical digests
    proc2 = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                    "--block-size", "16", "--base-port", str(base_port),
                    "--log-dir", str(tmp_path / "run2"))
    line2 = json.loads(proc2.stdout.strip().splitlines()[-1])
    assert line["grad_digest"] == line2["grad_digest"]
    assert line["param_digest"] == line2["param_digest"]


def test_inject_port_busy_reports_e_port(base_port, tmp_path):
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                   "--block-size", "16", "--base-port", str(base_port),
                   "--log-dir", str(tmp_path), "--inject", "port-busy")
    assert proc.returncode == 3
    assert json.loads(proc.stderr.strip())["code"] == "E_PORT"


def test_inject_bad_block_reports_e_block(base_port, tmp_path):
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                   "--base-port", str(base_port), "--log-dir", str(tmp_path),
                   "--inject", "bad-block")
    assert proc.returncode == 4
    assert json.loads(proc.stderr.strip())["code"] == "E_BLOCK"


def test_inject_corrupt_snapshot_reports_e_snapshot(base_port, tmp_path):
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                   "--block-size", "16", "--base-port", str(base_port),
                   "--log-dir", str(tmp_path),
                   "--snapshot", str(tmp_path / "snap.json"),
                   "--inject", "corrupt-snapshot")
    assert proc.returncode == 5
    assert json.loads(proc.stderr.strip())["code"] == "E_SNAPSHOT"


def test_invalid_seed_reports_e_init(base_port, tmp_path):
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "-3",
                   "--block-size", "16", "--base-port", str(base_port),
                   "--log-dir", str(tmp_path))
    assert proc.returncode == 2
    assert json.loads(proc.stderr.strip())["code"] == "E_INIT"


def test_verify_command(base_port, tmp_path):
    log_dir = str(tmp_path / "runv")
    proc = run_cli("run", "--workers", "4", "--dim", "64", "--seed", "42",
                   "--block-size", "16", "--base-port", str(base_port),
                   "--log-dir", log_dir)
    assert proc.returncode == 0, proc.stderr
    verify = run_cli("verify", "--log-dir", log_dir)
    assert verify.returncode == 0
    assert json.loads(verify.stdout.strip())["consistent"] is True
