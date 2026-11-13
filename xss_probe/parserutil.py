"""HTML 回显上下文解析：判断标记出现在脚本、属性还是文本上下文。

不使用浏览器引擎，而是用确定性的字符串/分词规则，保证同一响应永远得到
同一结论，可重复复核。规则：

1. 先扫描 ``<script>...</script>`` 区间（大小写不敏感），落在区间内的标记
   归为“脚本”上下文；
2. 其余位置，若标记落在某个标签 ``<...>`` 内部（其左侧最近的 ``<`` 比
   最近的 ``>`` 更靠近），归为“属性”上下文；
3. 否则归为“文本”上下文。
"""

from __future__ import annotations

import html as _html
import re

from .models import CTX_ATTR, CTX_SCRIPT, CTX_TEXT

_SCRIPT_OPEN = re.compile(r"<script\b[^>]*>", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_TAG_OPEN = re.compile(r"<[a-zA-Z!/]")


def escaped_forms(marker: str) -> list[str]:
    """返回标记的标准 HTML 转义形式（覆盖引号转义与否两种常见写法）。"""
    return [
        _html.escape(marker, quote=True),   # & < > " ' 全部转义
        _html.escape(marker, quote=False),  # 仅转义 & < >
    ]


def _script_ranges(document: str) -> list[tuple[int, int]]:
    """返回所有 script 元素的 (起点, 终点) 区间，终点为 </script> 之后。"""
    ranges: list[tuple[int, int]] = []
    for match in _SCRIPT_OPEN.finditer(document):
        close = _SCRIPT_CLOSE.search(document, match.end())
        end = close.end() if close else len(document)
        ranges.append((match.start(), end))
    return ranges


def _inside_any(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in ranges)


def classify_contexts(document: str, marker: str) -> list[str]:
    """列出标记在响应体中每次未转义出现所属的上下文（去重保序）。"""
    if not marker:
        return []
    script_ranges = _script_ranges(document)
    contexts: list[str] = []
    search_from = 0
    while True:
        index = document.find(marker, search_from)
        if index < 0:
            break
        if _inside_any(index, script_ranges):
            ctx = CTX_SCRIPT
        else:
            last_open = document.rfind("<", 0, index)
            last_close = document.rfind(">", 0, index)
            # 标签内：左侧最近的是标签起始 <，且看起来是真正的元素标签
            if last_open > last_close and _TAG_OPEN.match(document, last_open):
                ctx = CTX_ATTR
            else:
                ctx = CTX_TEXT
        if ctx not in contexts:
            contexts.append(ctx)
        search_from = index + len(marker)
    return contexts
