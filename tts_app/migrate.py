"""v1 与 v2/v3 的双向迁移器。

升级（-> v2/v3）：补全风格标签、韵律缩放、精度档与时长缩放默认值，
逐项记录补全来源（filled_defaults）。
降级（-> v1）：对 v1 无法表达的嵌套风格继承与逐音素时长曲线，
按显式声明的 drop / flatten 规则处理并输出丢弃明细（dropped）。
"""

import copy

from .config import DEFAULTS
from .errors import E_SEMANTIC_DRIFT, TtsError

# v1 无法表达的配置项
V1_INEXPRESSIBLE = ("style.inherit", "prosody.duration_curve", "prosody.duration_scale")


def _fill(cfg, path, value, source, filled):
    filled.append({"field": path, "value": value, "source": source})
    return value


def upgrade(cfg, target_version):
    """v1/v2 -> v2/v3。返回 (新配置, 迁移记录)。"""
    if target_version not in ("v2", "v3"):
        raise TtsError(E_SEMANTIC_DRIFT, f"不支持的目标版本 {target_version}",
                       target=target_version)
    out = copy.deepcopy(cfg)
    src = cfg["source_version"]
    filled = []
    style = out["style"]
    prosody = out["prosody"]
    if not style.get("tag"):
        style["tag"] = _fill(out, "style.tag", DEFAULTS["style"],
                             "default:style", filled)
    if prosody.get("scale") is None:
        prosody["scale"] = _fill(out, "prosody.scale", DEFAULTS["prosody_scale"],
                                 "default:prosody_scale", filled)
    if not out.get("precision"):
        out["precision"] = _fill(out, "precision", DEFAULTS["precision"],
                                 "default:precision", filled)
    if prosody.get("duration_scale") is None:
        prosody["duration_scale"] = _fill(
            out, "prosody.duration_scale", DEFAULTS["duration_scale"],
            "default:duration_scale", filled)
    record = {
        "source_version": src,
        "target_version": target_version,
        "filled_defaults": filled,
        "dropped": [],
    }
    out["source_version"] = target_version
    return out, record


def downgrade_to_v1(cfg, rules=None):
    """v2/v3 -> v1。rules 显式声明 drop 或 flatten，未声明则报错（禁止静默）。

    rules 示例: {"style.inherit": "drop", "prosody.duration_curve": "flatten"}
    """
    rules = rules or {}
    out = copy.deepcopy(cfg)
    src = cfg["source_version"]
    dropped = []
    style = out["style"]
    prosody = out["prosody"]

    if style.get("inherit"):
        rule = rules.get("style.inherit")
        if rule != "drop":
            raise TtsError(E_SEMANTIC_DRIFT,
                           "v1 无法表达 style.inherit 且未声明 drop 规则",
                           unrecoverable=["style.inherit"])
        dropped.append({"field": "style.inherit", "value": style["inherit"],
                        "rule": "drop"})
        style["inherit"] = None

    curve = prosody.get("duration_curve")
    if curve:
        rule = rules.get("prosody.duration_curve")
        if rule == "flatten":
            flat = sum(curve) / len(curve)
            dropped.append({"field": "prosody.duration_curve",
                            "value": list(curve), "rule": "flatten",
                            "flattened_to": flat})
            prosody["duration_scale"] = (prosody.get("duration_scale") or 1.0) * flat
            prosody["duration_curve"] = None
        elif rule == "drop":
            dropped.append({"field": "prosody.duration_curve",
                            "value": list(curve), "rule": "drop"})
            prosody["duration_curve"] = None
        else:
            raise TtsError(E_SEMANTIC_DRIFT,
                           "v1 无法表达 prosody.duration_curve 且未声明 drop/flatten 规则",
                           unrecoverable=["prosody.duration_curve"])

    record = {
        "source_version": src,
        "target_version": "v1",
        "filled_defaults": [],
        "dropped": dropped,
    }
    out["source_version"] = "v1"
    return out, record


def migrate(cfg, target_version, rules=None):
    """双向迁移入口，产物携带来源版本与目标版本。"""
    src = cfg["source_version"]
    if src == target_version:
        return copy.deepcopy(cfg), {
            "source_version": src, "target_version": target_version,
            "filled_defaults": [], "dropped": []}
    if target_version == "v1":
        return downgrade_to_v1(cfg, rules)
    return upgrade(cfg, target_version)
