"""展陈脚本语义解析。

完全基于中文关键词与数值修饰语的规则解析，不调用任何外部模型或网络接口。
每条文本输出一个动作对象，无法识别的文本抛出 :class:`UnparsedEntry`，
由上层流水线转换为业务错误码 E_UNPARSED。
"""

import re

from .enums import ActionType, Direction, Easing


class UnparsedEntry(ValueError):
    """单条脚本无法解析，携带条目索引与原文。"""

    def __init__(self, index: int, text: str, reason: str):
        super().__init__(f"条目 {index} 无法解析: {reason}")
        self.index = index
        self.text = text
        self.reason = reason


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 轴关键词 -> 轴向
_AXIS_WORDS = (
    (re.compile(r"(x轴|水平轴|横轴)", re.IGNORECASE), "X"),
    (re.compile(r"(y轴|竖直轴|上下轴)", re.IGNORECASE), "Y"),
    (re.compile(r"(z轴|竖轴|垂直轴)", re.IGNORECASE), "Z"),
)

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _to_number(token: str) -> float:
    token = token.strip()
    try:
        return float(token)
    except ValueError:
        if token in _CN_DIGITS:
            return float(_CN_DIGITS[token])
        # 十X / X十 / 一百 等简单组合
        if token.startswith("十") and len(token) == 2:
            return 10.0 + _CN_DIGITS.get(token[1], 0)
        if token.endswith("十") and len(token) == 2:
            return _CN_DIGITS.get(token[0], 1) * 10.0
        if "百" in token:
            head = token.split("百", 1)[0]
            return (_CN_DIGITS.get(head, 1) * 100.0)
    raise ValueError(f"无法识别数值: {token}")


def _extract_number(patterns, text: str, default: float) -> float:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _to_number(match.group(1))
    match = _NUMBER_RE.search(text)
    if match:
        return float(match.group(1))
    return default


def _detect_axis(text: str, default: str = "Y") -> str:
    for pattern, axis in _AXIS_WORDS:
        if pattern.search(text):
            return axis
    return default


def _detect_easing(text: str, default: Easing) -> Easing:
    if re.search(r"线性|匀速", text):
        return Easing.LINEAR
    if re.search(r"正弦", text):
        return Easing.SINE
    if re.search(r"缓入缓出|缓入|缓出|平滑加减速", text):
        return Easing.EASE_IN_OUT
    return default


def _detect_period(text: str) -> float:
    # 显式时长：N 秒 / 周期 N 秒 / 用时 N 秒
    match = re.search(r"(?:周期|时长|用时|耗时)?\s*(\d+(?:\.\d+)?)\s*秒", text)
    if match:
        return float(match.group(1))
    # 速度修饰语调整默认周期（秒）
    if re.search(r"快速|迅速", text):
        return 2.0
    if re.search(r"缓慢|慢慢|缓缓", text):
        return 8.0
    return 4.0


def _detect_sign(text: str) -> int:
    # 正向/逆时针 为 +1；反向/顺时针 为 -1（右手坐标系约定）
    if re.search(r"反向|逆向|倒转|顺时针", text):
        return -1
    return 1


def parse_entry(text: str, index: int = 0) -> dict:
    """解析单条脚本文本为结构化动作字典。"""
    raw = text.strip()
    if not raw:
        raise UnparsedEntry(index, text, "空条目")

    action_type = None
    direction = None
    default_amplitude = 360.0
    amplitude = None

    # 1) 展开动作：展开/外扩/张开
    if re.search(r"展开|外扩|张开", raw):
        action_type = ActionType.EXPAND
        direction = Direction.EXPAND
        amplitude = _extract_number(
            [r"(?:外扩|展开|张开)\s*(\d+(?:\.\d+)?)\s*(?:厘米|cm|米|m)?"],
            raw, 0.3,
        )

    # 2) 摆动动作：来回摆 / 往复摆 / 摇摆
    elif re.search(r"来回摆|往复摆|摇摆|来回转动|往复转动", raw):
        axis = _detect_axis(raw, default="X")
        action_type = ActionType.SWING
        direction = Direction[f"SWING_{axis}"]
        amplitude = _extract_number(
            [r"摆[动幅]?\s*(?:角)?\s*(\d+(?:\.\d+)?)\s*度",
             r"(\d+(?:\.\d+)?)\s*度"],
            raw, 30.0,
        )

    # 3) 旋转动作：旋转/转一周/自转
    elif re.search(r"旋转|自转|转动|转一周|转一圈|转一圈", raw):
        axis = _detect_axis(raw, default="Y")
        action_type = ActionType.ROTATE
        direction = Direction[f"ROT_{axis}"]
        if re.search(r"一周|一圈|整圈|360", raw):
            amplitude = 360.0
        elif re.search(r"半周|半圈|180", raw):
            amplitude = 180.0
        else:
            amplitude = _extract_number(
                [r"旋转\s*(\d+(?:\.\d+)?)\s*度", r"(\d+(?:\.\d+)?)\s*度"],
                raw, 360.0,
            )

    # 4) 平移动作：平移/移动/滑动/位移
    elif re.search(r"平移|移动|滑动|位移", raw):
        axis = _detect_axis(raw, default="X")
        action_type = ActionType.TRANSLATE
        direction = Direction[f"MOVE_{axis}"]
        amplitude = _extract_number(
            [r"(?:平移|移动|滑动|位移)\s*(\d+(?:\.\d+)?)\s*(厘米|cm|米|m)?",
             r"(\d+(?:\.\d+)?)\s*(厘米|cm|米|m)"],
            raw, 1.0,
        )
        # 单位归一化为米
        if re.search(r"\d+(?:\.\d+)?\s*(厘米|cm)", raw):
            amplitude /= 100.0

    else:
        raise UnparsedEntry(index, raw, "未匹配到旋转/摆动/平移/展开关键词")

    period = _detect_period(raw)
    easing = _detect_easing(raw, default=Easing.LINEAR)
    sign = _detect_sign(raw)
    if action_type in (ActionType.ROTATE, ActionType.TRANSLATE):
        amplitude = abs(amplitude) * sign

    return {
        "index": index,
        "raw_text": raw,
        "action_type": action_type.value,
        "direction": direction.value,
        "amplitude": float(amplitude),
        "period": float(period),
        "easing": easing.value,
    }


def parse_script(entries) -> list:
    """解析脚本数组（字符串列表或 JSON 中的 text 字段），返回动作对象列表。"""
    results = []
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            text = entry.get("text") or entry.get("script") or ""
        else:
            text = str(entry)
        results.append(parse_entry(text, index))
    return results
