"""阶段一：查询归一化。

全半角折叠、英文大小写折叠、连续空白合并、标点剥离。
输出归一化文本、被修改位置列表（基于原始文本下标）与阶段指纹。
空查询或仅含标点的查询抛出 StageError(E_EMPTY_QUERY)。
"""
from __future__ import annotations

import unicodedata

from .errors import E_EMPTY_QUERY, E_MALFORMED_QUERY, StageError
from .hashing import fingerprint

STAGE = "normalize"


def _fold_width(ch: str) -> str:
    code = ord(ch)
    if code == 0x3000:  # 全角空格
        return " "
    if 0xFF01 <= code <= 0xFF5E:  # 全角可见字符
        return chr(code - 0xFEE0)
    return ch


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def normalize_text(raw) -> dict:
    if raw is None or not isinstance(raw, str):
        raise StageError(E_MALFORMED_QUERY, "query text is missing or not a string")

    modified: list[dict] = []
    # 第一遍：全半角折叠 + 英文大小写折叠
    folded: list[tuple[int, str]] = []
    for idx, ch in enumerate(raw):
        out = _fold_width(ch)
        if out != ch:
            modified.append({"pos": idx, "op": "width_fold", "from": ch, "to": out})
        if "A" <= out <= "Z":
            lowered = out.lower()
            modified.append({"pos": idx, "op": "case_fold", "from": out, "to": lowered})
            out = lowered
        folded.append((idx, out))

    # 第二遍：标点剥离
    no_punct: list[tuple[int, str]] = []
    for idx, ch in folded:
        if _is_punct(ch):
            modified.append({"pos": idx, "op": "punct_strip", "from": ch, "to": None})
        else:
            no_punct.append((idx, ch))

    # 第三遍：连续空白合并为单个空格，并去除首尾空白
    chars: list[str] = []
    prev_space = True  # 起始视为空白，等价于左 strip
    last_space_pos = -1  # 最近一次被保留为空格字符的原始位置
    for idx, ch in no_punct:
        if ch.isspace():
            if prev_space:
                modified.append({"pos": idx, "op": "ws_collapse", "from": ch, "to": None})
            else:
                chars.append(" ")
                last_space_pos = idx
            prev_space = True
        else:
            chars.append(ch)
            prev_space = False
    # 右 strip：末尾保留的空格需删除并补记
    if chars and chars[-1] == " ":
        chars.pop()
        modified.append({"pos": last_space_pos, "op": "ws_collapse", "from": " ", "to": None})

    text = "".join(chars)
    if not text:
        raise StageError(E_EMPTY_QUERY, "query is empty or punctuation-only after normalization")

    modified.sort(key=lambda m: (m["pos"], m["op"]))
    result = {"text": text, "modified_positions": modified}
    result["fingerprint"] = fingerprint(result)
    return result
