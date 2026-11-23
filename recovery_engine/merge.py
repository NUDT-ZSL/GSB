"""跨版本合并与统一恢复计划构建。

同一服务在两代配置中同时存在时：
- 先比较配置声明的逻辑时钟（v1 配置级 logical_clock_ms vs v2 服务级 effective_ms），
  数值大者为最新版本；
- 逻辑时钟相同则按来源格式优先级裁决（v2 > v1）。
合并后每条动作保留来源版本、原始时间表示与原始相对回退值；
裁决依据以稳定排序 JSON 输出，保证任意运行次数下逐位一致。
"""

from .models import Plan

# 来源格式优先级：数字大者优先
FORMAT_PRIORITY = {"v2": 2, "v1": 1}


def merge(v1_cfg, v2_cfg):
    """合并两代配置为统一恢复计划。

    v1_cfg / v2_cfg: 各代加载器返回的 {"services": {...}, ...}；允许为 None。
    """
    v1_services = (v1_cfg or {}).get("services", {})
    v2_services = (v2_cfg or {}).get("services", {})
    v1_clock = (v1_cfg or {}).get("config_clock_ms", 0)

    plan = Plan()
    for name in sorted(set(v1_services) | set(v2_services)):
        in_v1 = name in v1_services
        in_v2 = name in v2_services
        if in_v1 and in_v2:
            s1, s2 = v1_services[name], v2_services[name]
            if v1_clock != s2.effective_ms:
                winner, loser = (s2, s1) if s2.effective_ms > v1_clock else (s1, s2)
                reason = "newer_logical_clock"
            else:
                if FORMAT_PRIORITY[s2.source_version] >= FORMAT_PRIORITY[s1.source_version]:
                    winner, loser = s2, s1
                else:
                    winner, loser = s1, s2
                reason = "format_priority"
            plan.services[name] = winner
            plan.rationale.append({
                "service": name,
                "decision": winner.source_version,
                "reason": reason,
                "v1_clock_ms": v1_clock,
                "v2_effective_ms": s2.effective_ms,
                "v2_generation": s2.generation,
                "discarded": loser.source_version,
            })
        elif in_v1:
            plan.services[name] = v1_services[name]
            plan.rationale.append({
                "service": name, "decision": "v1", "reason": "only_in_v1",
                "v1_clock_ms": v1_clock, "v2_effective_ms": 0, "v2_generation": 0,
                "discarded": "",
            })
        else:
            plan.services[name] = v2_services[name]
            plan.rationale.append({
                "service": name, "decision": "v2", "reason": "only_in_v2",
                "v1_clock_ms": 0, "v2_effective_ms": v2_services[name].effective_ms,
                "v2_generation": v2_services[name].generation,
                "discarded": "",
            })
    return plan
