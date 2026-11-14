"""修复后的脱敏实现。

与修复前实现的关键差异：
  * 先做 NFKC 规范化、剔除零宽字符，再做匹配；
  * 手机号支持 +86 前缀、空格 / 连字符 / 点 / 中点 / 下划线分隔、全角写法；
  * 证件号支持分隔符、全角字符、小写 x 校验位与 15 位旧式号码；
  * 号码区间映射回原始文本后再替换，因此只改动号码本身，日志其余内容
    （包括换行与原有排版）保持不变。
"""

from __future__ import annotations

from sanitizers.common import (
    ID_TOKEN,
    PHONE_TOKEN,
    find_sensitive_in_normalized,
    normalize_with_offsets,
)

TOKENS = {"phone": PHONE_TOKEN, "id_card": ID_TOKEN}


def sanitize(text: str) -> str:
    normalized, original_starts, original_ends = normalize_with_offsets(text)
    matches = find_sensitive_in_normalized(normalized)
    if not matches:
        return text

    pieces: list[str] = []
    cursor = 0
    for match in matches:
        start = original_starts[match.start]
        end = original_ends[match.end - 1]
        if start < cursor:  # 防御性处理：区间不应重叠
            continue
        pieces.append(text[cursor:start])
        pieces.append(TOKENS[match.kind])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)
