"""业务参数合规校验（要求 9）。

与"结构校验"不同：这里处理的是 JSON 格式合法、但业务上不合规的参数，
例如空用户标识、非数字订单号。校验不通过的用例直接标记为 rejected，
不向本地实例发起任何请求，并记录逐条拒绝原因。
"""

from __future__ import annotations

from typing import Any

_RULE_LABELS = {
    "not_empty": "必须为非空字符串",
    "numeric": "必须为数字（不允许字母/符号）",
    "positive_int": "必须为正整数",
}


class ParamValidationError(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _as_text(value: Any) -> str:
    # 查询参数统一按字符串处理（HTTP 语义），True/False 不接受为业务参数
    if isinstance(value, bool):
        return str(value)
    return str(value)


def check_param_rules(params: dict[str, Any], param_rules: dict[str, list[str]]) -> None:
    """按用例声明的 param_rules 校验参数；不合规即抛出 ParamValidationError。"""
    reasons: list[str] = []
    for name, rule_list in param_rules.items():
        if name not in params:
            reasons.append(f"参数 {name!r} 缺失，{_RULE_LABELS[rule_list[0]]}")
            continue
        raw = params[name]
        text = _as_text(raw).strip()
        for rule in rule_list:
            if rule == "not_empty":
                if text == "":
                    reasons.append(f"参数 {name!r} 为空，{_RULE_LABELS[rule]}")
            elif rule == "numeric":
                try:
                    float(text)
                except ValueError:
                    reasons.append(
                        f"参数 {name!r} 的值 {text!r} 不合规，{_RULE_LABELS[rule]}"
                    )
            elif rule == "positive_int":
                int_value: int | None = None
                try:
                    int_value = int(text)
                except ValueError:
                    pass
                if int_value is None or str(int_value) != text or int_value <= 0:
                    reasons.append(
                        f"参数 {name!r} 的值 {text!r} 不合规，{_RULE_LABELS[rule]}"
                    )
    if reasons:
        raise ParamValidationError(reasons)
