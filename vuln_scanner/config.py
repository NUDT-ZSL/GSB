"""目标清单与扫描策略的加载、校验。

校验失败时抛出 ConfigError，CLI 统一捕获后以退出码 1 结束并打印全部问题。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

# 三类检测项的稳定标识，策略文件与报告中都使用这些 ID
CHECK_SQLI = "sql_injection"
CHECK_XSS = "xss"
CHECK_TRAVERSAL = "path_traversal"
ALL_CHECKS = (CHECK_SQLI, CHECK_XSS, CHECK_TRAVERSAL)

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class EndpointSpec:
    """单个注入点描述。parameter 为 None 表示路径型注入点。"""

    path: str
    checks: tuple[str, ...]
    parameter: str | None = None
    placeholder: str | None = None  # 路径型注入点在 path 中的占位符，如 {file}


@dataclass
class TargetSpec:
    name: str
    base_url: str
    endpoints: list[EndpointSpec] = field(default_factory=list)


@dataclass
class Policy:
    timeout_seconds: float
    enabled_checks: frozenset[str]


# 样本服务的默认注入点画像（目标清单未给 endpoints 时使用）
DEFAULT_ENDPOINTS: list[EndpointSpec] = [
    EndpointSpec("/login", (CHECK_SQLI,), "username"),
    EndpointSpec("/search", (CHECK_XSS,), "q"),
    EndpointSpec("/search", (CHECK_XSS,), "lang"),
    EndpointSpec("/comment", (CHECK_XSS,), "text"),
    EndpointSpec("/file", (CHECK_TRAVERSAL,), "name"),
    EndpointSpec("/files/{file}", (CHECK_TRAVERSAL,), None, "{file}"),
    EndpointSpec("/error", (CHECK_TRAVERSAL,), "name"),
]


def _load_json(path: str, label: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ConfigError(["%s 文件不存在: %s" % (label, path)])
    except json.JSONDecodeError as exc:
        raise ConfigError(["%s 不是合法 JSON: %s" % (label, exc)])
    if not isinstance(data, dict):
        raise ConfigError(["%s 顶层必须是 JSON 对象" % label])
    return data


def _valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def load_targets(path: str) -> list[TargetSpec]:
    data = _load_json(path, "目标清单")
    problems: list[str] = []

    if "targets" not in data:
        problems.append("目标清单缺少必需字段: targets")
        raise ConfigError(problems)
    raw_targets = data["targets"]
    if not isinstance(raw_targets, list):
        raise ConfigError(["targets 必须是数组"])
    if len(raw_targets) > 10:
        problems.append("目标数量上限为 10 个，当前为 %d 个" % len(raw_targets))

    targets: list[TargetSpec] = []
    invalid_urls: list[str] = []
    for index, item in enumerate(raw_targets):
        where = "targets[%d]" % index
        if not isinstance(item, dict):
            problems.append("%s 必须是对象" % where)
            continue
        missing = [key for key in ("name", "base_url") if key not in item]
        if missing:
            problems.append("%s 缺少必需字段: %s"
                            % (where, ", ".join(missing)))
            continue
        if not _valid_url(str(item["base_url"])):
            invalid_urls.append("%s (name=%s)" % (item["base_url"], item["name"]))
            continue
        endpoints = _parse_endpoints(item.get("endpoints"), where, problems)
        targets.append(TargetSpec(str(item["name"]), str(item["base_url"]),
                                  endpoints))

    if invalid_urls:
        problems.append("以下 URL 不是合法的 http/https 地址:\n  - "
                        + "\n  - ".join(invalid_urls))
    if problems:
        raise ConfigError(problems)
    return targets


def _parse_endpoints(raw, where: str, problems: list[str]) -> list[EndpointSpec]:
    if raw is None:
        return list(DEFAULT_ENDPOINTS)
    if not isinstance(raw, list):
        problems.append("%s.endpoints 必须是数组" % where)
        return list(DEFAULT_ENDPOINTS)

    endpoints: list[EndpointSpec] = []
    for i, item in enumerate(raw):
        pos = "%s.endpoints[%d]" % (where, i)
        if not isinstance(item, dict) or "path" not in item or "checks" not in item:
            problems.append("%s 缺少必需字段: path/checks" % pos)
            continue
        checks = item["checks"]
        if not isinstance(checks, list) or not checks:
            problems.append("%s.checks 必须是非空数组" % pos)
            continue
        unknown = [c for c in checks if c not in ALL_CHECKS]
        if unknown:
            problems.append("%s.checks 含未知检测项: %s" % (pos, ", ".join(unknown)))
            continue
        parameter = item.get("parameter")
        placeholder = item.get("placeholder")
        endpoints.append(EndpointSpec(
            path=str(item["path"]),
            checks=tuple(checks),
            parameter=None if parameter is None else str(parameter),
            placeholder=None if placeholder is None else str(placeholder),
        ))
    return endpoints


def load_policy(path: str) -> Policy:
    data = _load_json(path, "扫描策略")
    problems: list[str] = []

    if "timeout_seconds" not in data:
        problems.append("扫描策略缺少必需字段: timeout_seconds")
    if "enabled_checks" not in data:
        problems.append("扫描策略缺少必需字段: enabled_checks")
    if problems:
        raise ConfigError(problems)

    timeout = data["timeout_seconds"]
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or timeout <= 0:
        raise ConfigError(["timeout_seconds 必须是正数"])

    raw_checks = data["enabled_checks"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ConfigError(["enabled_checks 必须是非空数组"])
    unknown = [c for c in raw_checks if c not in ALL_CHECKS]
    if unknown:
        raise ConfigError(["enabled_checks 含未知检测项: %s" % ", ".join(unknown)])

    return Policy(timeout_seconds=float(timeout),
                  enabled_checks=frozenset(raw_checks))
