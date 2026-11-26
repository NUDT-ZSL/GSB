"""JSONL parsing task loading."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import Codes, DomainError


REQUIRED_FIELDS = ("sentence_id", "raw_text", "token_list", "pos_tag_list", "lang")


def load_tasks(path: str | Path) -> list[dict]:
    p = Path(path)
    tasks: list[dict] = []
    try:
        handle = p.open("r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise DomainError(f"tasks file not found: {p}", code=Codes.E_NOT_FOUND, path=str(p)) from exc

    with handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DomainError(
                    f"invalid JSONL at line {line_number}: {exc.msg}",
                    code=Codes.E_FORMAT_CORRUPT,
                    field=f"tasks.jsonl:{line_number}:{exc.colno}",
                ) from exc
            if not isinstance(row, dict):
                raise DomainError(
                    f"task row must be an object at line {line_number}",
                    code=Codes.E_FORMAT_CORRUPT,
                    field=f"tasks.jsonl:{line_number}",
                )
            for field in REQUIRED_FIELDS:
                if field not in row:
                    raise DomainError(
                        f"task missing field {field}",
                        code=Codes.E_FORMAT_CORRUPT,
                        field=f"line_{line_number}.{field}",
                    )
            tasks.append(row)

    if not tasks:
        raise DomainError("task JSONL contains no rows", code=Codes.E_FORMAT_CORRUPT, field="tasks.jsonl")
    return tasks
