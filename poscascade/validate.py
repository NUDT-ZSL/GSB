from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import object_checksum, text_checksum
from .config import PipelineConfig
from .errors import BusinessError, ErrorCode, ErrorCollector

PUNCT_CLASSES = {"none", "terminal", "comma", "quote", "space"}


def read_input_lines(
    path: str | Path,
    config: PipelineConfig,
    errors: ErrorCollector,
) -> tuple[list[dict[str, Any]], str]:
    raw_rows: list[dict[str, Any]] = []
    expected_line_no = 1
    seen_doc_ids: set[str] = set()

    try:
        handle = Path(path).open("r", encoding="utf-8")
    except OSError as exc:
        errors.add(
            ErrorCode.E_INPUT_FORMAT,
            f"输入文件读取失败: {exc}",
            details={"path": str(path)},
        )
        return [], object_checksum([])

    with handle:
        for file_line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                errors.add(
                    ErrorCode.E_INPUT_FORMAT,
                    f"JSONL 第 {file_line_no} 行不是合法 JSON: {exc.msg}",
                    details={"file_line_no": file_line_no},
                )
                expected_line_no += 1
                continue
            if not isinstance(record, dict):
                errors.add(
                    ErrorCode.E_INPUT_FORMAT,
                    f"JSONL 第 {file_line_no} 行必须是对象",
                    details={"file_line_no": file_line_no},
                )
                expected_line_no += 1
                continue

            doc_id = record.get("doc_id")
            line_no = record.get("line_no")
            text = record.get("text")
            punct_class = record.get("punct_class", "none")

            if not isinstance(doc_id, str) or not doc_id:
                errors.add(
                    ErrorCode.E_INPUT_FORMAT,
                    "doc_id 必须是非空字符串",
                    line_no=line_no if isinstance(line_no, int) else None,
                    details={"file_line_no": file_line_no, "doc_id": doc_id},
                )
            elif doc_id in seen_doc_ids:
                errors.add(
                    ErrorCode.E_DUP_DOC,
                    f"重复 doc_id: {doc_id}",
                    doc_id=doc_id,
                    line_no=line_no if isinstance(line_no, int) else expected_line_no,
                )
            else:
                seen_doc_ids.add(doc_id)

            if not isinstance(line_no, int) or isinstance(line_no, bool):
                errors.add(
                    ErrorCode.E_LINE_NO,
                    "line_no 必须是整数",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    details={"file_line_no": file_line_no, "line_no": line_no},
                )
            elif line_no != expected_line_no:
                errors.add(
                    ErrorCode.E_LINE_NO,
                    f"line_no 非连续: 期望 {expected_line_no}，实际 {line_no}",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    line_no=line_no,
                    details={"expected": expected_line_no},
                )

            if not isinstance(text, str):
                errors.add(
                    ErrorCode.E_TEXT_EMPTY,
                    "text 必须是非空字符串",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    line_no=line_no if isinstance(line_no, int) else expected_line_no,
                    details={"file_line_no": file_line_no},
                )
            elif not text.strip():
                errors.add(
                    ErrorCode.E_TEXT_EMPTY,
                    "text 为空或仅包含空白",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    line_no=line_no if isinstance(line_no, int) else expected_line_no,
                )
            elif len(text) > config.max_codepoints_per_line:
                errors.add(
                    ErrorCode.E_RESOURCE_LIMIT,
                    "单行码点数超过上限",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    line_no=line_no if isinstance(line_no, int) else expected_line_no,
                    stage="validate",
                    details={
                        "codepoint_len": len(text),
                        "max_codepoints_per_line": config.max_codepoints_per_line,
                    },
                )

            if not isinstance(punct_class, str) or punct_class not in PUNCT_CLASSES:
                errors.add(
                    ErrorCode.E_INPUT_FORMAT,
                    "punct_class 不在允许集合中",
                    doc_id=doc_id if isinstance(doc_id, str) else None,
                    line_no=line_no if isinstance(line_no, int) else expected_line_no,
                    details={
                        "punct_class": punct_class,
                        "allowed": sorted(PUNCT_CLASSES),
                    },
                )

            if isinstance(record, dict):
                raw_rows.append(record)
            expected_line_no += 1

    checksum = object_checksum(raw_rows)
    return raw_rows, checksum


def run_validation(
    raw_rows: list[dict[str, Any]],
    input_checksum: str,
    config: PipelineConfig,
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    for record in raw_rows:
        text = record["text"]
        codepoints = list(text)
        lines.append(
            {
                "doc_id": record["doc_id"],
                "line_no": record["line_no"],
                "text": text,
                "punct_class": record["punct_class"],
                "codepoints": codepoints,
                "codepoint_len": len(codepoints),
                "text_sha256": text_checksum(text),
            }
        )
    return {
        "stage": "validate",
        "config_checksum": config.checksum,
        "input_checksum": input_checksum,
        "line_count": len(lines),
        "lines": lines,
    }


def validate_offsets(
    text: str,
    tokens: list[dict[str, Any]],
    *,
    doc_id: str | None = None,
    line_no: int | None = None,
    stage: str = "segment",
) -> None:
    cursor = 0
    for token_index, token in enumerate(tokens):
        try:
            word = token["word"]
            start = token["start"]
            end = token["end"]
        except (KeyError, TypeError) as exc:
            raise BusinessError(
                ErrorCode.E_OFFSET_LEN,
                "词条缺少 word/start/end 字段",
                doc_id=doc_id,
                line_no=line_no,
                stage=stage,
                details={"token_index": token_index},
            ) from exc
        if (
            not isinstance(word, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise BusinessError(
                ErrorCode.E_OFFSET_LEN,
                "word/start/end 类型不合法",
                doc_id=doc_id,
                line_no=line_no,
                stage=stage,
                details={"token_index": token_index},
            )
        if start != cursor:
            raise BusinessError(
                ErrorCode.E_OFFSET_LEN,
                "词条起始偏移与前一词条结束偏移不连续",
                doc_id=doc_id,
                line_no=line_no,
                stage=stage,
                details={
                    "token_index": token_index,
                    "expected_start": cursor,
                    "actual_start": start,
                },
            )
        if end < start or end - start != len(word):
            raise BusinessError(
                ErrorCode.E_OFFSET_LEN,
                "偏移长度与词条码点长度不一致",
                doc_id=doc_id,
                line_no=line_no,
                stage=stage,
                details={
                    "token_index": token_index,
                    "start": start,
                    "end": end,
                    "word_codepoint_len": len(word),
                },
            )
        if end > len(text) or text[start:end] != word:
            raise BusinessError(
                ErrorCode.E_OFFSET_LEN,
                "偏移切片与词条内容不一致",
                doc_id=doc_id,
                line_no=line_no,
                stage=stage,
                details={
                    "token_index": token_index,
                    "start": start,
                    "end": end,
                    "word": word,
                },
            )
        cursor = end
    if cursor != len(text):
        raise BusinessError(
            ErrorCode.E_OFFSET_LEN,
            "词条偏移未覆盖全行码点",
            doc_id=doc_id,
            line_no=line_no,
            stage=stage,
            details={"covered": cursor, "codepoint_len": len(text)},
        )
