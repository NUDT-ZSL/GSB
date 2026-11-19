"""JSONL sample input and deterministic report serialization."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import IntentError
from .models import QuerySample


def read_samples(path: str | Path) -> list[QuerySample]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            f"无法读取样本文件：{path}",
            stage="input",
            field="samples_path",
            path=str(path),
            reason=str(exc),
        ) from exc
    except UnicodeDecodeError as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            "样本文件不是合法 UTF-8",
            stage="input",
            field="encoding",
            field_offset=exc.start,
        ) from exc

    samples: list[QuerySample] = []
    seen: dict[str, int] = {}
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntentError(
                "E_CONFIG_INVALID",
                "样本行不是合法 JSON",
                stage="input",
                line_no=line_no,
                field="json",
                field_offset=exc.pos,
            ) from exc
        if not isinstance(row, dict):
            raise IntentError(
                "E_CONFIG_INVALID",
                "样本行必须是 JSON 对象",
                stage="input",
                line_no=line_no,
                field="row",
            )
        for field in ("query_id", "query_text", "locale", "label_raw"):
            if field not in row:
                raise IntentError(
                    "E_CONFIG_INVALID",
                    f"样本缺少字段：{field}",
                    stage="input",
                    line_no=line_no,
                    field=field,
                )
            if not isinstance(row[field], str):
                raise IntentError(
                    "E_CONFIG_INVALID",
                    f"样本字段必须为字符串：{field}",
                    stage="input",
                    line_no=line_no,
                    field=field,
                )
        query_id = row["query_id"].strip()
        query_text = row["query_text"]
        locale = row["locale"].strip()
        label_raw = row["label_raw"].strip()
        if not query_id:
            raise IntentError(
                "E_CONFIG_INVALID",
                "query_id 不能为空",
                stage="input",
                line_no=line_no,
                field="query_id",
            )
        if not query_text:
            raise IntentError(
                "E_CONFIG_INVALID",
                "query_text 不能为空",
                stage="input",
                line_no=line_no,
                field="query_text",
            )
        if not locale:
            raise IntentError(
                "E_CONFIG_INVALID",
                "locale 不能为空",
                stage="input",
                line_no=line_no,
                field="locale",
            )
        if not label_raw:
            raise IntentError(
                "E_CONFIG_INVALID",
                "label_raw 不能为空",
                stage="input",
                line_no=line_no,
                field="label_raw",
            )
        if query_id in seen:
            raise IntentError(
                "E_DUP_QUERY",
                f"query_id 重复：{query_id}",
                stage="input",
                line_no=line_no,
                query_id=query_id,
                field="query_id",
                first_line_no=seen[query_id],
            )
        seen[query_id] = line_no
        samples.append(
            QuerySample(
                query_id=query_id,
                query_text=query_text,
                locale=locale,
                label_raw=label_raw,
                line_no=line_no,
            )
        )
    if not samples:
        raise IntentError(
            "E_CONFIG_INVALID",
            "样本文件为空",
            stage="input",
            field="samples",
        )
    return samples


def json_dumps(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def write_json_atomic(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(json_dumps(payload), encoding="utf-8")
    os.replace(temporary, target)
