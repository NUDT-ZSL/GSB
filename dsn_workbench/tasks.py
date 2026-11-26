from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import E_FORMAT_CORRUPT, WorkbenchError
from .util import (
    content_hash,
    read_jsonl_file,
    sorted_sentence_ids,
)


REQUIRED_TASK_FIELDS = (
    "sentence_id",
    "raw_text",
    "token_list",
    "pos_tag_list",
    "lang",
    "domain",
)


def validate_tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                f"task row {index} is not an object",
                {"row": index},
            )
        missing = [field for field in REQUIRED_TASK_FIELDS if field not in row]
        if missing:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "task row is missing required fields",
                {"row": index, "missing": missing},
            )
        sentence_id = row["sentence_id"]
        if isinstance(sentence_id, bool) or not isinstance(
            sentence_id, (str, int)
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "sentence_id must be a string or integer",
                {"row": index, "value": sentence_id},
            )
        sentence_id = str(sentence_id)
        if sentence_id in seen:
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "duplicate sentence_id",
                {"row": index, "sentence_id": sentence_id},
            )
        seen.add(sentence_id)
        raw_text = row["raw_text"]
        if not isinstance(raw_text, str):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "raw_text must be a string",
                {"row": index, "sentence_id": sentence_id},
            )
        token_list = row["token_list"]
        pos_tag_list = row["pos_tag_list"]
        if not isinstance(token_list, list) or not all(
            isinstance(value, str) for value in token_list
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "token_list must be a string array",
                {"row": index, "sentence_id": sentence_id},
            )
        if not isinstance(pos_tag_list, list) or not all(
            isinstance(value, str) for value in pos_tag_list
        ):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "pos_tag_list must be a string array",
                {"row": index, "sentence_id": sentence_id},
            )
        if len(token_list) != len(pos_tag_list):
            raise WorkbenchError(
                E_FORMAT_CORRUPT,
                "token_list and pos_tag_list length mismatch",
                {
                    "row": index,
                    "sentence_id": sentence_id,
                    "token_count": len(token_list),
                    "pos_count": len(pos_tag_list),
                },
            )
        for field in ("lang", "domain"):
            if not isinstance(row[field], str) or not row[field]:
                raise WorkbenchError(
                    E_FORMAT_CORRUPT,
                    f"{field} must be a non-empty string",
                    {"row": index, "sentence_id": sentence_id},
                )
        tasks.append(
            {
                "sentence_id": sentence_id,
                "raw_text": raw_text,
                "token_list": list(token_list),
                "pos_tag_list": list(pos_tag_list),
                "lang": row["lang"],
                "domain": row["domain"],
            }
        )
    order = {sentence_id: index for index, sentence_id in enumerate(sorted_sentence_ids(list(seen)))}
    tasks.sort(key=lambda task: order[task["sentence_id"]])
    return tasks


def load_tasks(path: str | Path) -> list[dict[str, Any]]:
    return validate_tasks(read_jsonl_file(Path(path)))


def task_digest(tasks: list[dict[str, Any]]) -> str:
    return content_hash(
        [
            {
                "sentence_id": task["sentence_id"],
                "raw_text": task["raw_text"],
                "token_list": task["token_list"],
                "pos_tag_list": task["pos_tag_list"],
                "lang": task["lang"],
                "domain": task["domain"],
            }
            for task in tasks
        ]
    )

