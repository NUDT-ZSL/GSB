"""词项归一化、文档解析与每文档词频/位置统计。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .errors import E_DUP_DOC_ID, E_EMPTY_DOC, E_PARSE_INVALID, WorkbenchError

REQUIRED_FIELDS = ("doc_id", "title", "body", "source")


def fold_halfwidth(text: str) -> str:
    """全角 ASCII/标点折叠为半角，全角空格折叠为普通空格。"""
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\u3000":
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return "".join(chars)


def normalize_text(text: str) -> str:
    """全半角折叠 -> 大小写折叠 -> 空白合并。"""
    folded = fold_halfwidth(text)
    folded = folded.casefold()
    return " ".join(folded.split())


def tokenize(text: str) -> list[str]:
    """归一化后按空白切分为词项，位置即词项序号（从 0 开始）。"""
    return normalize_text(text).split()


@dataclass(frozen=True)
class ParsedDocument:
    doc_id: str
    title: str
    source: str
    tokens: tuple[str, ...]
    term_freq: dict[str, int]
    positions: dict[str, tuple[int, ...]]
    raw_line: str

    def length(self) -> int:
        return len(self.tokens)


def _stats(tokens: list[str]) -> tuple[dict[str, int], dict[str, tuple[int, ...]]]:
    term_freq: dict[str, int] = {}
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        term_freq[token] = term_freq.get(token, 0) + 1
        positions.setdefault(token, []).append(index)
    return term_freq, {term: tuple(pos) for term, pos in positions.items()}


def parse_document(
    raw_line: str,
    seen_doc_ids: set[str] | None = None,
    *,
    line_no: int | None = None,
) -> ParsedDocument:
    """解析单行 JSONL 文档，失败时抛出带业务码的 WorkbenchError。"""
    identifier = f"line:{line_no}" if line_no is not None else "line:?"
    try:
        payload = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise WorkbenchError(
            f"非法 JSON: {exc.msg}",
            stage="parse",
            identifier=identifier,
        ) from exc

    if not isinstance(payload, dict):
        raise WorkbenchError(
            "文档顶层结构必须为 JSON 对象",
            stage="parse",
            identifier=identifier,
        )

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise WorkbenchError(
            f"缺少必填字段: {','.join(missing)}",
            stage="parse",
            identifier=identifier,
        )

    doc_id = str(payload["doc_id"])
    identifier = doc_id
    if not all(isinstance(payload[field], str) for field in REQUIRED_FIELDS):
        raise WorkbenchError(
            "doc_id/title/body/source 必须均为字符串",
            stage="parse",
            identifier=identifier,
        )

    if seen_doc_ids is not None and doc_id in seen_doc_ids:
        raise WorkbenchError(
            f"文档 ID 重复: {doc_id}",
            stage="parse",
            identifier=identifier,
        )

    tokens = tokenize(payload["body"])
    if not tokens:
        raise WorkbenchError(
            f"正文归一化后为空: {doc_id}",
            stage="parse",
            identifier=identifier,
        )

    term_freq, positions = _stats(tokens)
    if seen_doc_ids is not None:
        seen_doc_ids.add(doc_id)
    return ParsedDocument(
        doc_id=doc_id,
        title=payload["title"],
        source=payload["source"],
        tokens=tuple(tokens),
        term_freq=term_freq,
        positions=positions,
        raw_line=raw_line.rstrip("\n"),
    )


def parse_stream(
    lines: list[str] | tuple[str, ...],
) -> tuple[list[ParsedDocument], list[dict[str, Any]]]:
    """批量解析文档流，返回成功文档与失败清单，重复 ID 标记 E_DUP_DOC_ID。"""
    seen: set[str] = set()
    documents: list[ParsedDocument] = []
    failures: list[dict[str, Any]] = []
    for line_no, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            documents.append(parse_document(raw_line, seen, line_no=line_no))
        except WorkbenchError as exc:
            code = _classify_parse_error(exc)
            from .errors import make_failure

            failures.append(make_failure(exc.identifier or identifier_of(raw_line, line_no),
                                         "parse", code, exc).to_dict())
    return documents, failures


def identifier_of(raw_line: str, line_no: int) -> str:
    try:
        payload = json.loads(raw_line)
        if isinstance(payload, dict) and "doc_id" in payload:
            return str(payload["doc_id"])
    except json.JSONDecodeError:
        pass
    return f"line:{line_no}"


def _classify_parse_error(exc: WorkbenchError) -> str:
    message = str(exc)
    if message.startswith("正文归一化后为空"):
        return E_EMPTY_DOC
    if message.startswith("文档 ID 重复"):
        return E_DUP_DOC_ID
    return E_PARSE_INVALID
