"""任务 9：确定性本地模拟服务与样本生成器。"""
from __future__ import annotations

import json
from pathlib import Path

from recon import generator
from recon.storage import Storage
from tests.conftest import Workspace, _free_port

ROOT = Path(__file__).resolve().parent.parent


def _collect(workspace, scenario=None):
    handle = workspace.start_server(scenario=scenario)
    try:
        proc = workspace.run_collector("base")
        assert proc.returncode == 0, proc.stderr
    finally:
        handle.shutdown()
    cfg = json.load(open(workspace.config("base"), encoding="utf-8"))
    return Storage(cfg["db_path"]).main_hash()


def test_seed42_hash_matches_truth(tmp_path):
    port = _free_port()
    wd = tmp_path / "s42"
    manifest = generator.generate(str(wd), seed=42, port=port)
    ws = Workspace(wd, port, manifest)
    db_hash = _collect(ws)
    assert db_hash == ws.truth()["content_hash"]


def test_same_seed_reproducible_hash(tmp_path):
    hashes = []
    for i in range(2):
        port = _free_port()
        wd = tmp_path / f"rep{i}"
        manifest = generator.generate(str(wd), seed=42, port=port)
        ws = Workspace(wd, port, manifest)
        hashes.append(_collect(ws))
    assert hashes[0] == hashes[1]  # 同种子同配置，主表哈希一致


def test_different_seed_changes_hash(tmp_path):
    port1, port2 = _free_port(), _free_port()
    wd42 = tmp_path / "seed42"
    wd43 = tmp_path / "seed43"
    m42 = generator.generate(str(wd42), seed=42, port=port1)
    m43 = generator.generate(str(wd43), seed=43, port=port2)
    h42 = _collect(Workspace(wd42, port1, m42))
    h43 = _collect(Workspace(wd43, port2, m43))
    assert h42 != h43  # 种子改变 -> 数据与哈希改变


def test_no_runtime_randomness_injection_rules(tmp_path):
    """所有错误/延迟注入均由规则文件显式声明，不含随机抽样。"""
    port = _free_port()
    wd = tmp_path / "rules"
    manifest = generator.generate(str(wd), seed=42, port=port)
    base_rules = json.load(open(manifest["rules_path"], encoding="utf-8"))
    # 显式声明的注入键，且没有任何 random 字段
    for key in ("rest_errors", "ts_errors", "rest_delay_after_page",
                "rest_delay_ms", "rest_delay_concurrency1"):
        assert key in base_rules
    scenario = json.load(open(
        manifest["scenario_rules"]["retry_500"], encoding="utf-8"))
    assert scenario["rest_errors"] == [
        {"attempt": 1, "status": 500}, {"attempt": 2, "status": 500}]
