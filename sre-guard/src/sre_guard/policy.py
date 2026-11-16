"""策略加载、强校验与版本演进 diff。"""
from __future__ import annotations

from typing import Any

from .errors import E_POLICY, GovernanceError, Issue
from .models import (
    BreakerPolicy,
    DegradeTarget,
    Policy,
    RateLimitPolicy,
    ServicePolicy,
)
from .validation import is_strict_int, locate_line, parse_json_file, service_line


def _err(issues: list[Issue], path: str, message: str,
         text: str, key: str, service_hint: str | None = None,
         value: Any = None) -> None:
    line = locate_line(text, key, service_hint)
    issues.append(Issue(path, message, line=line, value=value))


def _validate_service(service_id: Any, raw: Any, text: str,
                      issues: list[Issue], seen: set[str]) -> ServicePolicy | None:
    path = f"services.{service_id}"
    if not isinstance(service_id, str) or not service_id:
        issues.append(Issue("services", "服务标识必须为非空字符串", value=service_id))
        return None
    if service_id in seen:
        issues.append(Issue(path, "服务标识重复", line=service_line(text, service_id), value=service_id))
        return None
    seen.add(service_id)
    if not isinstance(raw, dict):
        issues.append(Issue(path, "服务策略必须为对象", line=service_line(text, service_id)))
        return None

    rl_raw = raw.get("rate_limit")
    rl = _validate_rate_limit(rl_raw, path, text, service_id, issues)
    cb_raw = raw.get("breaker")
    cb = _validate_breaker(cb_raw, path, text, service_id, issues)
    chain = _validate_degrade_chain(raw.get("degrade_chain", []), path, text, service_id, issues)
    if rl is None or cb is None:
        return None
    return ServicePolicy(service_id=service_id, rate_limit=rl, breaker=cb, degrade_chain=chain)


def _validate_rate_limit(raw: Any, path: str, text: str, service_id: str,
                         issues: list[Issue]) -> RateLimitPolicy | None:
    base = f"{path}.rate_limit"
    if not isinstance(raw, dict):
        issues.append(Issue(base, "rate_limit 必须为对象", line=locate_line(text, "rate_limit", service_id)))
        return None
    rate = raw.get("rate_per_second")
    burst = raw.get("burst_capacity")
    if not is_strict_int(rate) or rate <= 0:
        _err(issues, f"{base}.rate_per_second", "限流速率必须为正整数",
             text, "rate_per_second", service_id, rate)
    if not is_strict_int(burst) or burst < 0:
        _err(issues, f"{base}.burst_capacity", "突发容量必须为非负整数",
             text, "burst_capacity", service_id, burst)
    if not is_strict_int(rate) or rate <= 0 or not is_strict_int(burst) or burst < 0:
        return None
    return RateLimitPolicy(rate_per_second=rate, burst_capacity=burst)


def _validate_breaker(raw: Any, path: str, text: str, service_id: str,
                      issues: list[Issue]) -> BreakerPolicy | None:
    base = f"{path}.breaker"
    if not isinstance(raw, dict):
        issues.append(Issue(base, "breaker 必须为对象", line=locate_line(text, "breaker", service_id)))
        return None
    window = raw.get("window_minutes")
    threshold = raw.get("failure_threshold_percent")
    min_count = raw.get("min_request_count")
    probe = raw.get("half_open_probe")
    cooldown = raw.get("cooldown_ms", 60_000)

    if not is_strict_int(window) or window <= 0:
        _err(issues, f"{base}.window_minutes", "熔断窗口分钟数必须为正整数",
             text, "window_minutes", service_id, window)
    if not is_strict_int(threshold) or not 0 <= threshold <= 100:
        _err(issues, f"{base}.failure_threshold_percent",
             "失败率阈值必须为 0 至 100 的整数百分比",
             text, "failure_threshold_percent", service_id, threshold)
    if not is_strict_int(min_count) or min_count <= 0:
        _err(issues, f"{base}.min_request_count", "最小请求数必须为正整数",
             text, "min_request_count", service_id, min_count)
    if not is_strict_int(probe) or probe <= 0:
        _err(issues, f"{base}.half_open_probe", "半开试探数必须为正整数",
             text, "half_open_probe", service_id, probe)
    if not is_strict_int(cooldown) or cooldown <= 0:
        _err(issues, f"{base}.cooldown_ms", "冷却时长必须为正整数毫秒",
             text, "cooldown_ms", service_id, cooldown)

    invalid = (
        not is_strict_int(window) or window <= 0
        or not is_strict_int(threshold) or not 0 <= threshold <= 100
        or not is_strict_int(min_count) or min_count <= 0
        or not is_strict_int(probe) or probe <= 0
        or not is_strict_int(cooldown) or cooldown <= 0
    )
    if invalid:
        return None
    return BreakerPolicy(
        window_minutes=window,
        failure_threshold_percent=threshold,
        min_request_count=min_count,
        half_open_probe=probe,
        cooldown_ms=cooldown,
    )


def _validate_degrade_chain(raw: Any, path: str, text: str, service_id: str,
                            issues: list[Issue]) -> list[DegradeTarget]:
    base = f"{path}.degrade_chain"
    if raw is None:
        return []
    if not isinstance(raw, list):
        issues.append(Issue(base, "降级链必须为数组", line=locate_line(text, "degrade_chain", service_id)))
        return []
    chain: list[DegradeTarget] = []
    seen_targets: set[str] = set()
    weight_sum = 0
    for index, item in enumerate(raw):
        item_path = f"{base}[{index}]"
        if not isinstance(item, dict):
            issues.append(Issue(item_path, "降级目标必须为对象"))
            continue
        target = item.get("target")
        weight = item.get("weight")
        permille = item.get("success_permille", 1000)
        if not isinstance(target, str) or not target:
            _err(issues, f"{item_path}.target", "降级目标标识必须为非空字符串",
                 text, "target", service_id, target)
            continue
        if target in seen_targets:
            _err(issues, f"{item_path}.target", "降级链目标不得重复",
                 text, "target", service_id, target)
            continue
        if not is_strict_int(weight) or weight < 0:
            _err(issues, f"{item_path}.weight", "权重必须为非负整数",
                 text, "weight", service_id, weight)
            continue
        if not is_strict_int(permille) or not 0 <= permille <= 1000:
            _err(issues, f"{item_path}.success_permille",
                 "名义成功概率必须为 0 至 1000 的千分比整数",
                 text, "success_permille", service_id, permille)
            continue
        seen_targets.add(target)
        weight_sum += weight
        chain.append(DegradeTarget(target=target, weight=weight, success_permille=permille))
    if not chain:
        issues.append(Issue(base, "降级链为空或全部非法", line=locate_line(text, "degrade_chain", service_id)))
    elif weight_sum <= 0:
        _err(issues, base, "降级链权重和必须为正", text, "weight", service_id, weight_sum)
    return chain


def load_policy(path: str) -> Policy:
    """从本地 JSON 加载并整体强校验策略；任一非法以 E_POLICY 整体拒绝。"""
    data, text = parse_json_file(path)
    issues: list[Issue] = []
    if not isinstance(data, dict):
        raise GovernanceError(E_POLICY, "策略根必须为 JSON 对象",
                              [Issue("$", "期望 object", value=type(data).__name__)])
    version = data.get("version")
    if not is_strict_int(version) or version < 1:
        _err(issues, "version", "策略版本号必须为 >=1 的整数", text, "version", value=version)
    services_raw = data.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        issues.append(Issue("services", "services 必须为非空对象",
                            line=locate_line(text, "services")))
        if issues:
            raise GovernanceError(E_POLICY, "策略校验失败，整体拒绝加载", issues)
    services: dict[str, ServicePolicy] = {}
    seen: set[str] = set()
    for service_id, service_raw in services_raw.items():
        service = _validate_service(service_id, service_raw, text, issues, seen)
        if service is not None:
            services[service.service_id] = service
    if issues:
        raise GovernanceError(E_POLICY, "策略校验失败，整体拒绝加载", issues)
    return Policy(version=version, services=services)


def bump_version(policy: Policy, overrides: dict[str, dict[str, Any]]) -> Policy:
    """基于覆盖字段生成单调递增的新版本策略（用于版本演进示例）。"""
    services = {sid: _apply_override(sp, overrides.get(sid, {}))
                for sid, sp in policy.services.items()}
    return Policy(version=policy.version + 1, services=services)


def _apply_override(sp: ServicePolicy, override: dict[str, Any]) -> ServicePolicy:
    if not override:
        return sp
    breaker = sp.breaker
    if "failure_threshold_percent" in override:
        breaker = BreakerPolicy(
            window_minutes=breaker.window_minutes,
            failure_threshold_percent=override["failure_threshold_percent"],
            min_request_count=breaker.min_request_count,
            half_open_probe=breaker.half_open_probe,
            cooldown_ms=breaker.cooldown_ms,
        )
    return ServicePolicy(service_id=sp.service_id, rate_limit=sp.rate_limit,
                         breaker=breaker, degrade_chain=sp.degrade_chain)


def diff_policies(old: Policy, new: Policy) -> list[dict[str, Any]]:
    """辨识两个策略版本的字段级差异。"""
    diffs: list[dict[str, Any]] = []
    for service_id in sorted(set(old.services) | set(new.services)):
        old_sp = old.services.get(service_id)
        new_sp = new.services.get(service_id)
        if old_sp is None or new_sp is None:
            diffs.append({"service": service_id, "field": "$",
                          "change": "added" if new_sp else "removed"})
            continue
        for field_name in ("rate_limit", "breaker", "degrade_chain"):
            old_value = getattr(old_sp, field_name)
            new_value = getattr(new_sp, field_name)
            if old_value != new_value:
                diffs.append({
                    "service": service_id,
                    "field": field_name,
                    "old": _safe_repr(old_value),
                    "new": _safe_repr(new_value),
                })
    return diffs


def _safe_repr(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return asdict(value)
    if isinstance(value, list):
        from dataclasses import asdict
        return [asdict(item) if hasattr(item, "__dataclass_fields__") else item for item in value]
    return value
