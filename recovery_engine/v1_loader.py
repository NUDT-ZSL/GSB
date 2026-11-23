"""v1 单文件 JSON 恢复配置的解析与校验。

校验规则：
- 服务名唯一；
- 相对回退秒数为非负整数；
- 动作脚本步骤在注入逻辑时钟上非降序（at_tick 单调）；
- 扁平 service_actions 映射与动作声明一致。
非法输入抛 BizError(E_BAD_CONFIG)，且不写入任何持久化状态。
"""

import json

from .errors import BizError, ErrCode
from .models import Action, Service


def _require(cond, message, **details):
    if not cond:
        raise BizError(ErrCode.E_BAD_CONFIG, message, **details)


def load_v1(path):
    """加载并校验 v1 配置，返回 dict: name -> Service（source_version='v1'）及配置逻辑时钟。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    _require(raw.get("format") == "recovery-v1", "v1 配置 format 字段非法", got=raw.get("format"))
    _require(isinstance(raw.get("logical_clock_ms"), int), "v1 缺少配置级逻辑时钟 logical_clock_ms")
    config_clock_ms = raw["logical_clock_ms"]

    services = {}
    for entry in raw.get("services", []):
        name = entry.get("name")
        _require(isinstance(name, str) and name, "v1 服务名缺失或非法", entry=entry)
        _require(name not in services, "v1 服务名重复", service=name)

        fb = entry.get("fallback_seconds")
        _require(isinstance(fb, int) and not isinstance(fb, bool) and fb >= 0,
                 "v1 相对回退秒数必须为非负整数", service=name, got=fb)

        actions = []
        prev_tick = None
        for idx, a in enumerate(entry.get("actions", [])):
            tick = a.get("at_tick")
            _require(isinstance(tick, int) and not isinstance(tick, bool),
                     "v1 动作 at_tick 必须为整数", service=name, action=a.get("id"))
            if prev_tick is not None:
                _require(tick >= prev_tick,
                         "v1 动作步骤在逻辑时钟上必须非降序",
                         service=name, action=a.get("id"), prev=prev_tick, got=tick)
            prev_tick = tick
            actions.append(Action(
                action_id=a["id"], kind=a.get("kind", "switch"), at_tick=tick,
                script=a.get("script", ""), source_version="v1", seq=idx,
            ))

        services[name] = Service(
            name=name,
            failure_domain=entry.get("failure_domain", ""),
            actions=actions,
            upstreams=list(entry.get("upstreams", [])),
            source_version="v1",
            fallback_seconds=fb,
            ladder=list(entry.get("ladder", [10, 25, 50, 100])),
        )

    # 扁平 service_actions 映射与动作声明一致
    mapping = raw.get("service_actions", {})
    _require(set(mapping) == set(services), "v1 扁平映射与动作声明不一致",
             mapping=sorted(mapping), declared=sorted(services))
    for name, ids in mapping.items():
        declared = [a.action_id for a in services[name].actions]
        _require(sorted(ids) == sorted(declared),
                 "v1 扁平映射的动作集合与声明不一致",
                 service=name, mapping=sorted(ids), declared=sorted(declared))

    return {"services": services, "config_clock_ms": config_clock_ms}
