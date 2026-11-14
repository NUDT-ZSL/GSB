"""修复前的脱敏实现（故意留有漏洞，用于对照）。

模拟一类真实事故：第一版实现"看起来能用"，但只覆盖了最规整的写法。

缺陷清单：
  1. 手机号只匹配连续 11 位数字，带空格 / 连字符 / 点的分隔写法全部漏掉；
  2. 证件号只匹配连续 18 位数字，且不认小写 x 结尾；
  3. 完全不做 Unicode 规范化，全角数字写法漏掉；
  4. 不做 +86 前缀处理。

注意：这是演示用的"坏实现"，不要用于生产。
"""

from __future__ import annotations

import re

# 只认连续 11 位、1[3-9] 开头。分隔符、全角、+86 都不在射程内。
_BUGGY_PHONE_RE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")

# 只认连续 18 位、纯数字结尾；大写 X 也不在射程内。
_BUGGY_ID_RE = re.compile(r"(?<![0-9])[0-9]{17}[0-9](?![0-9])")

PHONE_TOKEN = "[手机号已脱敏]"
ID_TOKEN = "[证件号已脱敏]"


def sanitize(text: str) -> str:
    text = _BUGGY_PHONE_RE.sub(PHONE_TOKEN, text)
    text = _BUGGY_ID_RE.sub(ID_TOKEN, text)
    return text
