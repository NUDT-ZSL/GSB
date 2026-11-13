"""JSON 配置加载与严格边界校验。

校验失败抛出 ConfigError，错误码稳定：
E_CFG_PAGE / E_CFG_QPS / E_CFG_CONCURRENCY / E_CFG_INTERVAL /
E_CFG_RETRY / E_CFG_PATH。

校验阶段不创建任何数据库、日志、锁文件——调用方必须先 load_config
再初始化任何运行期资源。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .errors import ConfigError


def _is_int(value) -> bool:
    # bool 是 int 的子类，显式排除
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool))


@dataclass
class SourceConfig:
    name: str
    base_url: str
    page_size: int


@dataclass
class Config:
    rest_base_url: str
    ts_base_url: str
    csv_path: str
    page_size: int = 200
    qps: int = 5
    concurrency: int = 2
    ts_min_interval_ms: float = 60.0
    max_retries: int = 5
    backoff_base_ms: float = 100.0
    db_path: str = "data/recon.db"
    log_dir: str = "logs"
    lock_path: str = "data/recon.lock"
    state_path: str = "data/state.json"
    report_dir: str = "reports"
    control_file: str = "data/control"
    data_dir: str = "data"
    server_profile_path: str = "data/server_profile.json"
    # 生成器/测试使用的辅助字段
    extra: dict = field(default_factory=dict)

    @property
    def sources(self) -> dict[str, SourceConfig]:
        return {
            "rest": SourceConfig("rest", self.rest_base_url, self.page_size),
            "ts": SourceConfig("ts", self.ts_base_url, self.page_size),
            "csv": SourceConfig("csv", self.csv_path, self.page_size),
        }


def _require_nonempty_str(d: dict, key: str, cfg: dict):
    value = cfg.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError("E_CFG_PATH", f"字段 {key} 必须是非空字符串路径")
    setattr(d, key, value)


def load_config(path: str) -> Config:
    """加载并校验配置文件。任何失败均抛 ConfigError，不产生副作用文件。"""
    if not isinstance(path, str) or path.strip() == "":
        raise ConfigError("E_CFG_PATH", "配置文件路径不能为空")
    if not os.path.exists(path):
        raise ConfigError("E_CFG_PATH", f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError("E_CFG_PATH", f"配置文件不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("E_CFG_PATH", "配置根节点必须是 JSON 对象")

    c = Config(
        rest_base_url="", ts_base_url="", csv_path="",
    )

    # ---- 路径类字段（E_CFG_PATH） ----
    for key in ("rest_base_url", "ts_base_url", "csv_path", "db_path",
                "log_dir", "lock_path"):
        _require_nonempty_str(c, key, raw)
    for optional, default in (
        ("state_path", c.state_path), ("report_dir", c.report_dir),
        ("control_file", c.control_file), ("data_dir", os.path.dirname(c.db_path) or "data"),
        ("server_profile_path", c.server_profile_path),
    ):
        value = raw.get(optional, default)
        if not isinstance(value, str) or value.strip() == "":
            raise ConfigError("E_CFG_PATH", f"字段 {optional} 必须是非空字符串路径")
        setattr(c, optional, value)

    # ---- 分页大小：整数 1..200（E_CFG_PAGE） ----
    page_size = raw.get("page_size", 200)
    if not _is_int(page_size) or not (1 <= page_size <= 200):
        raise ConfigError("E_CFG_PAGE", "page_size 必须是 1 至 200 的整数")
    c.page_size = page_size

    # ---- QPS：> 0（E_CFG_QPS） ----
    qps = raw.get("qps", 5)
    if not _is_number(qps) or qps <= 0:
        raise ConfigError("E_CFG_QPS", "qps 必须大于 0")
    c.qps = qps

    # ---- 并发上限：>= 1（E_CFG_CONCURRENCY） ----
    concurrency = raw.get("concurrency", 2)
    if not _is_int(concurrency) or concurrency < 1:
        raise ConfigError("E_CFG_CONCURRENCY", "concurrency 必须是不小于 1 的整数")
    c.concurrency = concurrency

    # ---- 时间戳源最小间隔：>= 0（E_CFG_INTERVAL） ----
    interval = raw.get("ts_min_interval_ms", 50)
    if not _is_number(interval) or interval < 0:
        raise ConfigError("E_CFG_INTERVAL", "ts_min_interval_ms 必须不小于 0")
    c.ts_min_interval_ms = float(interval)

    # ---- 重试次数：>= 0；退避基数：>= 0（E_CFG_RETRY） ----
    max_retries = raw.get("max_retries", 5)
    if not _is_int(max_retries) or max_retries < 0:
        raise ConfigError("E_CFG_RETRY", "max_retries 必须是不小于 0 的整数")
    c.max_retries = max_retries
    base = raw.get("backoff_base_ms", 100)
    if not _is_number(base) or base < 0:
        raise ConfigError("E_CFG_RETRY", "backoff_base_ms 必须不小于 0")
    c.backoff_base_ms = float(base)

    known = {
        "rest_base_url", "ts_base_url", "csv_path", "page_size", "qps",
        "concurrency", "ts_min_interval_ms", "max_retries", "backoff_base_ms",
        "db_path", "log_dir", "lock_path", "state_path", "report_dir",
        "control_file", "data_dir", "server_profile_path",
    }
    c.extra = {k: v for k, v in raw.items() if k not in known}
    return c
