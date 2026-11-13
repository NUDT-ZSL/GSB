"""任务 11：配置加载与边界校验、稳定业务错误码、零副作用。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from recon.config import load_config
from recon.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent

ILLEGAL_CASES = [
    ("page_size", 0, "E_CFG_PAGE"),
    ("qps", -1, "E_CFG_QPS"),
    ("concurrency", 0, "E_CFG_CONCURRENCY"),
    ("ts_min_interval_ms", -10, "E_CFG_INTERVAL"),
    ("max_retries", -1, "E_CFG_RETRY"),
    ("db_path", "", "E_CFG_PATH"),
]


def _base_config(tmp_path: Path) -> dict:
    data_dir = tmp_path / "data"
    return {
        "rest_base_url": "http://127.0.0.1:8765/rest",
        "ts_base_url": "http://127.0.0.1:8765/ts",
        "csv_path": str(data_dir / "snapshot.csv"),
        "page_size": 200,
        "qps": 5,
        "concurrency": 2,
        "ts_min_interval_ms": 60,
        "max_retries": 5,
        "backoff_base_ms": 50,
        "db_path": str(data_dir / "recon.db"),
        "log_dir": str(tmp_path / "logs"),
        "lock_path": str(data_dir / "recon.lock"),
        "state_path": str(data_dir / "state.json"),
        "report_dir": str(tmp_path / "reports"),
        "control_file": str(data_dir / "control"),
        "data_dir": str(data_dir),
        "server_profile_path": str(data_dir / "server_profile.json"),
    }


def test_legal_config_loads(tmp_path):
    cfg_path = tmp_path / "legal.json"
    cfg_path.write_text(json.dumps(_base_config(tmp_path)), encoding="utf-8")
    cfg = load_config(str(cfg_path))
    assert cfg.page_size == 200
    assert cfg.qps == 5
    assert cfg.concurrency == 2


def test_each_illegal_config_reports_stable_code(tmp_path):
    for field, bad_value, expected_code in ILLEGAL_CASES:
        cfg_path = tmp_path / f"bad_{field}.json"
        raw = _base_config(tmp_path)
        raw[field] = bad_value
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")
        with __import__("pytest").raises(ConfigError) as exc:
            load_config(str(cfg_path))
        assert exc.value.code == expected_code, (field, exc.value.code)


def test_illegal_config_creates_no_files(tmp_path):
    # 通过 CLI 子进程验证：非零退出 + stderr 含错误码 + 无 db/log/lock 新建
    for field, bad_value, expected_code in ILLEGAL_CASES:
        case_dir = tmp_path / field
        case_dir.mkdir()
        cfg_path = case_dir / "bad.json"
        raw = _base_config(case_dir)
        raw[field] = bad_value
        # 把产物路径限定到该用例目录（db_path 非法用例除外，需保留其非法空值）
        if field != "db_path":
            raw["db_path"] = str(case_dir / "out" / "recon.db")
        raw["log_dir"] = str(case_dir / "logs")
        raw["lock_path"] = str(case_dir / "out" / "recon.lock")
        cfg_path.write_text(json.dumps(raw), encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "recon.cli", "--config", str(cfg_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode != 0, field
        assert expected_code in proc.stderr, (field, proc.stderr)
        # 校验失败不得创建数据库、日志、锁文件
        assert not (case_dir / "out").exists(), f"{field} 产生了数据目录"
        assert not (case_dir / "logs").exists(), f"{field} 产生了日志目录"
