"""历史拼接匹配器与修复后语义匹配器。

历史实现（legacy）的判定方式等价于::

    (pattern + " " + command).startswith(command)

即把规则文本与命令文本直接拼接成一个字符串，再用命令自身作为前缀去
比较。该实现存在两类确定性绕过：

1. 分隔符绕过：命令在首个 shell 分隔符（``;`` / ``&`` / ``|``）之后追加
   危险内容，legacy 只检查分隔符之前的命令头，尾部载荷被忽略。
2. 引号绕过：命令中出现的引号（``"`` / ``'``）被当作普通字符参与字面量
   比较，引号包裹不会被剥离，导致带引号的危险命令无法命中。

修复后（fixed）按规则自身语义执行：pattern 被编译为正则表达式，针对
**完整命令文本**进行 ``re.search``，不再做任何字符串拼接，也不截断
分隔符之后的内容。
"""

from __future__ import annotations

import re

# 历史实现只检查首个 shell 命令分隔符之前的“命令头”。
_SHELL_SEPARATORS = (";", "&", "|")


def legacy_head(command: str) -> str:
    """返回历史拼接匹配实际参与比较的命令片段（首个分隔符之前的部分）。"""
    cut = len(command)
    for sep in _SHELL_SEPARATORS:
        pos = command.find(sep)
        if pos != -1:
            cut = min(cut, pos)
    return command[:cut]


def legacy_matches(pattern: str, command: str) -> bool:
    """复现历史拼接匹配：(pattern + " " + command).startswith(command)。

    历史实现仅对分隔符之前的命令头做字面量子串判定，引号不做任何处理。
    """
    head = legacy_head(command)
    if head == "":
        return False
    glued = pattern + " " + command
    return glued.startswith(head)


def fixed_matches(compiled: re.Pattern[str], command: str) -> bool:
    """修复后语义匹配：对完整命令文本执行正则搜索，不拼接、不截断。"""
    return compiled.search(command) is not None
