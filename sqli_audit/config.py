"""目标描述文件的加载与严格校验。

验收要求（任务第 1 条）：字段缺失或类型错误时明确提示具体字段，
并且在配置合法之前绝不发起任何网络请求。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# 允许的参数位置
_LOCATIONS = ("query", "body")
# 允许的参数数据类型（当前仅支持字符串型参数，数值型可在后续版本扩展）
_TYPES = ("string",)
# 仅允许回环地址，确保自查全程不离开本机
_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class ConfigError(ValueError):
    """配置非法异常，message 中列出全部具体字段问题。"""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class ParamSpec:
    name: str
    location: str
    type: str
    label: str


@dataclass(frozen=True)
class TargetConfig:
    target_name: str
    base_url: str
    endpoint_path: str
    method: str
    baseline: dict
    params: list[ParamSpec]
    data_path: list[str]
    order_id_field: str
    timeout_seconds: int

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + self.endpoint_path

    def to_report_dict(self) -> dict:
        """以固定键序输出到报告（不含路径等环境相关信息）。"""
        return {
            "target_name": self.target_name,
            "base_url": self.base_url,
            "endpoint_path": self.endpoint_path,
            "method": self.method,
            "baseline": self.baseline,
            "params": [
                {
                    "name": p.name,
                    "location": p.location,
                    "type": p.type,
                    "label": p.label,
                }
                for p in self.params
            ],
            "data_path": list(self.data_path),
            "order_id_field": self.order_id_field,
            "timeout_seconds": self.timeout_seconds,
        }


def _is_loopback(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname
    if host is None:
        return False
    if host in _ALLOWED_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(path: str | Path) -> TargetConfig:
    """读取并校验目标描述文件。

    任何字段缺失/类型错误都会汇总进 ConfigError 一次性抛出，
    调用方在本函数成功返回前不得发起探测请求。
    """
    cfg_path = Path(path)
    problems: list[str] = []

    raw: object
    try:
        import json

        # utf-8-sig 同时兼容带/不带 BOM 的 UTF-8（Windows 编辑器常写入 BOM）
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError([f"目标描述文件不存在: {cfg_path}"])
    except OSError as exc:
        raise ConfigError([f"目标描述文件无法读取: {exc}"])
    except ValueError as exc:
        raise ConfigError([f"目标描述文件不是合法 JSON: {exc}"])

    if not isinstance(raw, dict):
        raise ConfigError(["目标描述文件顶层必须是 JSON 对象"])

    # ---- 顶层必填字段与类型 ----
    required_strings = ("target_name", "base_url", "endpoint_path", "method")
    for key in required_strings:
        if key not in raw:
            problems.append(f"缺失字段: {key}")
        elif not isinstance(raw[key], str) or not raw[key].strip():
            problems.append(f"字段类型错误: {key} 必须是非空字符串")

    if "baseline" not in raw:
        problems.append("缺失字段: baseline")
    elif not isinstance(raw["baseline"], dict):
        problems.append("字段类型错误: baseline 必须是对象")

    if "params" not in raw:
        problems.append("缺失字段: params")
    elif not isinstance(raw["params"], list) or not raw["params"]:
        problems.append("字段类型错误: params 必须是非空数组")

    # 可选字段的默认值
    data_path = raw.get("data_path", ["data", "orders"])
    order_id_field = raw.get("order_id_field", "order_id")
    timeout_seconds = raw.get("timeout_seconds", 5)

    if not isinstance(data_path, list) or not data_path or not all(
        isinstance(x, str) and x for x in data_path
    ):
        problems.append("字段类型错误: data_path 必须是非空字符串数组")
        data_path = ["data", "orders"]

    if not isinstance(order_id_field, str) or not order_id_field:
        problems.append("字段类型错误: order_id_field 必须是非空字符串")
        order_id_field = "order_id"

    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        problems.append("字段类型错误: timeout_seconds 必须是正整数")
        timeout_seconds = 5

    # ---- method / URL 语义校验 ----
    method = raw.get("method")
    if isinstance(method, str):
        if method.upper() not in ("GET", "POST"):
            problems.append(f"字段取值错误: method 仅支持 GET/POST，收到 {method!r}")
        else:
            method = method.upper()

    base_url = raw.get("base_url")
    endpoint_path = raw.get("endpoint_path")
    if isinstance(base_url, str) and base_url.strip():
        parsed = urlparse(base_url)
        if parsed.scheme != "http":
            problems.append("字段取值错误: base_url 必须使用 http 协议（本地自查禁止外网/HTTPS 目标）")
        elif not _is_loopback(base_url):
            problems.append("字段取值错误: base_url 主机必须是回环地址(127.0.0.1/localhost/::1)")
    if isinstance(endpoint_path, str) and endpoint_path and not endpoint_path.startswith("/"):
        problems.append("字段取值错误: endpoint_path 必须以 / 开头")

    # ---- baseline 内容校验 ----
    baseline = raw.get("baseline")
    if isinstance(baseline, dict):
        if not baseline:
            problems.append("字段内容错误: baseline 不能为空，至少需要一个正常订单号参数")
        for bk, bv in baseline.items():
            if not isinstance(bk, str) or not bk:
                problems.append("字段类型错误: baseline 的键必须是非空字符串")
                break
            if not isinstance(bv, str):
                problems.append(f"字段类型错误: baseline.{bk} 必须是字符串")

    # ---- params 逐项校验 ----
    params: list[ParamSpec] = []
    if isinstance(raw.get("params"), list) and raw["params"]:
        seen: set[tuple[str, str]] = set()
        for idx, item in enumerate(raw["params"]):
            prefix = f"params[{idx}]"
            if not isinstance(item, dict):
                problems.append(f"字段类型错误: {prefix} 必须是对象")
                continue
            ok = True
            for key in ("name", "location", "type", "label"):
                if key not in item:
                    problems.append(f"缺失字段: {prefix}.{key}")
                    ok = False
                elif not isinstance(item[key], str) or not item[key].strip():
                    problems.append(f"字段类型错误: {prefix}.{key} 必须是非空字符串")
                    ok = False
            if not ok:
                continue
            if item["location"] not in _LOCATIONS:
                problems.append(
                    f"字段取值错误: {prefix}.location 仅支持 {_LOCATIONS}，收到 {item['location']!r}"
                )
                continue
            if item["type"] not in _TYPES:
                problems.append(
                    f"字段取值错误: {prefix}.type 仅支持 {_TYPES}，收到 {item['type']!r}"
                )
                continue
            if method == "GET" and item["location"] == "body":
                problems.append(f"字段取值错误: {prefix}.location=body 与 method=GET 冲突")
                continue
            ident = (item["name"], item["location"])
            if ident in seen:
                problems.append(f"字段内容错误: {prefix} 与其他参数重复(name={item['name']}, location={item['location']})")
                continue
            seen.add(ident)
            params.append(
                ParamSpec(
                    name=item["name"],
                    location=item["location"],
                    type=item["type"],
                    label=item["label"],
                )
            )

    # baseline 键必须能在 params 中找到（保证基线请求可构造）
    if isinstance(baseline, dict) and params:
        param_names = {p.name for p in params}
        for bk in baseline:
            if isinstance(bk, str) and bk not in param_names:
                problems.append(f"字段内容错误: baseline.{bk} 在 params 中没有对应参数定义")

    if problems:
        # 去重并保持稳定顺序
        deduped = list(dict.fromkeys(problems))
        raise ConfigError(deduped)

    # 固定排序规则：参数按 (name, location) 排序，保证扫描顺序确定
    params.sort(key=lambda p: (p.name, p.location))

    return TargetConfig(
        target_name=raw["target_name"].strip(),
        base_url=raw["base_url"].strip(),
        endpoint_path=raw["endpoint_path"].strip(),
        method=method,
        baseline=dict(baseline),
        params=params,
        data_path=list(data_path),
        order_id_field=order_id_field,
        timeout_seconds=timeout_seconds,
    )
