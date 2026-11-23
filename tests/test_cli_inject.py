"""业务要求 8：CLI 子命令与三阶段异常注入的独立业务错误码。"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def _cli(*args, expect_code=0):
    proc = subprocess.run(
        [sys.executable, "-m", "reopt.cli", *args],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    payload = json.loads(proc.stdout)
    if expect_code == 0:
        assert proc.returncode == 0, proc.stderr + proc.stdout
    else:
        assert proc.returncode != 0, proc.stdout
        assert payload["code"] == expect_code, proc.stdout
    return payload


@pytest.fixture()
def cli_state(tmp_path):
    state_dir = str(tmp_path / "state")
    _cli("load", "--state", state_dir,
         "--tables", f"{SAMPLES}/tables.json",
         "--plan", f"{SAMPLES}/plan.json",
         "--batches", f"{SAMPLES}/batches.json",
         "--config", f"{SAMPLES}/config.json")
    return state_dir


def test_load_run_status_verify(cli_state):
    run_out = _cli("run", "--state", cli_state)
    assert run_out["committed"] == [1, 2, 3, 4, 5, 6]
    status = _cli("status", "--state", cli_state)
    assert status["watermark"] == 6
    assert status["form_version"] >= 2
    assert status["triggers"]
    assert status["ledger_entry_count"] > 0
    verify = _cli("verify", "--state", cli_state)
    assert verify["status"] == "verified"
    assert verify["checked_batches"] == 6


def test_resume_subcommand(cli_state):
    _cli("run", "--state", cli_state, "--until", "2")
    status = _cli("status", "--state", cli_state)
    assert status["watermark"] == 2
    resumed = _cli("resume", "--state", cli_state)
    assert 2 not in resumed["committed"]  # 只重放水位之后的批次
    assert _cli("verify", "--state", cli_state)["status"] == "verified"


def _write_manifest(tmp_path, injections):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"injections": injections}), encoding="utf-8")
    return str(path)


def test_inject_generate_digest_mismatch(cli_state, tmp_path):
    manifest = _write_manifest(tmp_path, [
        {"phase": "generate", "kind": "digest_mismatch", "batch_seq": 3}])
    out = _cli("inject", "--state", cli_state, "--manifest", manifest,
               expect_code=2001)
    assert out["code"] == 2001
    # 注入异常后批次 3 未提交，状态保持一致
    assert _cli("status", "--state", cli_state)["watermark"] == 2


def test_inject_observe_zero_sample(cli_state, tmp_path):
    manifest = _write_manifest(tmp_path, [
        {"phase": "observe", "kind": "zero_sample", "batch_seq": 4}])
    out = _cli("inject", "--state", cli_state, "--manifest", manifest,
               expect_code=2002)
    assert out["code"] == 2002
    assert _cli("status", "--state", cli_state)["watermark"] == 3


def test_inject_switch_illegal_budget(cli_state, tmp_path):
    manifest = _write_manifest(tmp_path, [
        {"phase": "switch", "kind": "illegal_budget", "value": -1}])
    out = _cli("inject", "--state", cli_state, "--manifest", manifest,
               expect_code=2003)
    assert out["code"] == 2003
    # 切换阶段异常：触发批次整体回滚，形态指针不变
    status = _cli("status", "--state", cli_state)
    assert status["form_version"] == 1
    assert status["watermark"] == 1


def test_inject_invalid_manifest(cli_state, tmp_path):
    manifest = _write_manifest(tmp_path, [
        {"phase": "generate", "kind": "zero_sample"}])
    out = _cli("inject", "--state", cli_state, "--manifest", manifest,
               expect_code=1013)
    assert out["code"] == 1013


def test_run_before_load_rejected(tmp_path):
    out = _cli("run", "--state", str(tmp_path / "empty"), expect_code=3004)
