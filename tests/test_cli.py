import json
from pathlib import Path

from hotcfg.cli import main

S = "samples"


def run_cli(argv):
    code = main(argv)
    return code


def test_run_verify_restore_replay(tmp_path):
    snap = str(tmp_path / "snap.json")
    log = str(tmp_path / "session.jsonl")
    code = run_cli(["--log", log, "run", "--commits", "4", "--queries", "5",
                    "--snapshot-out", snap])
    assert code == 0
    assert Path(snap).exists()
    assert Path(log).exists()
    # 结构化日志含提交与推送确认事件
    events = [json.loads(l)["event"] for l in Path(log).read_text().splitlines()]
    assert "commit" in events and "push_ack" in events

    assert run_cli(["verify", "--snapshot", snap]) == 0
    assert run_cli(["restore", "--snapshot", snap, "--continue-commits", "2"]) == 0
    assert run_cli(["replay", "--snapshot", snap]) == 0


def test_inject_corrupt_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(Path(f"{S}/config.json").read_text(encoding="utf-8"), encoding="utf-8")
    log = str(tmp_path / "inj.jsonl")
    code = run_cli(["--log", log, "inject", "corrupt-config", "--config", str(cfg)])
    assert code == 0  # 注入被捕获并以业务码报告
    records = [json.loads(l) for l in Path(log).read_text().splitlines()]
    assert records[-1]["error"]["code"] == "E_CONFIG"


def test_inject_missing_sub_ref(tmp_path):
    log = str(tmp_path / "inj.jsonl")
    code = run_cli(["--log", log, "inject", "missing-sub-ref"])
    assert code == 0
    records = [json.loads(l) for l in Path(log).read_text().splitlines()]
    assert records[-1]["error"]["code"] == "E_SUB"


def test_inject_push_timeout(tmp_path):
    log = str(tmp_path / "inj.jsonl")
    code = run_cli(["--log", log, "inject", "push-timeout",
                    "--ack-timeout-ms", "0.5", "--slow-ms", "20"])
    assert code == 0
    records = [json.loads(l) for l in Path(log).read_text().splitlines()]
    injected = [r for r in records if r.get("event") == "inject"]
    assert injected and injected[-1]["error"]["code"] == "E_PUSH"
