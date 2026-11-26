"""Stable serialization, hashing, and SQL lexical helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    BusinessError,
    E_INPUT_JSON,
    E_INPUT_SCHEMA,
    E_SQL_SYNTAX,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def read_json_file(path: str | Path) -> Any:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise BusinessError(E_INPUT_JSON, f"无法读取 JSON 文件: {source}", path=str(source), error=str(exc)) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_INPUT_JSON,
            f"JSON 解析失败: {source}",
            path=str(source),
            error=str(exc),
        ) from exc


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def require_dict(value: Any, field: str, *, view: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(E_INPUT_SCHEMA, f"{field} 必须是对象", view=view, field=field)
    return value


def require_list(value: Any, field: str, *, view: str | None = None) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(E_INPUT_SCHEMA, f"{field} 必须是数组", view=view, field=field)
    return value


def require_nonnegative_int(
    value: Any,
    field: str,
    *,
    code: str = E_INPUT_SCHEMA,
    view: str | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BusinessError(code, f"{field} 必须是非负整数", view=view, field=field, value=value)
    return value


def require_string(value: Any, field: str, *, view: str | None = None) -> str:
    if not isinstance(value, str):
        raise BusinessError(E_INPUT_SCHEMA, f"{field} 必须是字符串", view=view, field=field)
    return value


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}{sha256_json(value)}"


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_identifier(name: Any) -> bool:
    return isinstance(name, str) and bool(_IDENT_RE.match(name))


def strip_outer_quotes(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"', "`"):
        return value[1:-1]
    return value


def mask_sql_literals(sql: str, *, mask_comments: bool = True) -> str:
    """Blank literals/comments while preserving offsets and parentheses."""

    chars = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            start = index
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            for pos in range(start, index):
                if chars[pos] != "\n":
                    chars[pos] = " "
        elif char == '"':
            start = index
            index += 1
            while index < length:
                if sql[index] == '"':
                    if index + 1 < length and sql[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            for pos in range(start, index):
                if chars[pos] != "\n":
                    chars[pos] = " "
        elif char == "`":
            start = index
            index += 1
            while index < length and sql[index] != "`":
                index += 1
            index = min(index + 1, length)
            for pos in range(start, index):
                if chars[pos] != "\n":
                    chars[pos] = " "
        elif char == "[":
            start = index
            index += 1
            while index < length and sql[index] != "]":
                index += 1
            index = min(index + 1, length)
            for pos in range(start, index):
                if chars[pos] != "\n":
                    chars[pos] = " "
        elif mask_comments and char == "-" and index + 1 < length and sql[index + 1] == "-":
            start = index
            index += 2
            while index < length and sql[index] != "\n":
                index += 1
            for pos in range(start, index):
                chars[pos] = " "
        elif mask_comments and char == "/" and index + 1 < length and sql[index + 1] == "*":
            start = index
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            for pos in range(start, index):
                if chars[pos] != "\n":
                    chars[pos] = " "
        else:
            index += 1
    return "".join(chars)


def normalize_sql(sql: str) -> str:
    """Collapse whitespace outside literals while preserving literal content."""

    out: list[str] = []
    index = 0
    length = len(sql)
    pending_space = False

    def append_plain(value: str) -> None:
        nonlocal pending_space
        if pending_space and out:
            out.append(" ")
        pending_space = False
        out.append(value)

    def copy_quoted(start: int, quote: str, allow_double_escape: bool) -> int:
        nonlocal pending_space
        if pending_space and out:
            out.append(" ")
        pending_space = False
        position = start
        out.append(sql[position])
        position += 1
        while position < length:
            char = sql[position]
            out.append(char)
            position += 1
            if char == quote:
                if allow_double_escape and position < length and sql[position] == quote:
                    out.append(sql[position])
                    position += 1
                    continue
                break
        return position

    while index < length:
        char = sql[index]
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            pending_space = True
            index += 2
            while index < length and sql[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and sql[index + 1] == "*":
            pending_space = True
            index += 2
            while index + 1 < length and not (sql[index] == "*" and sql[index + 1] == "/"):
                index += 1
            index = min(index + 2, length)
            continue
        if char in {"'", '"', "`"}:
            index = copy_quoted(index, char, allow_double_escape=True)
            continue
        if char == "[":
            if pending_space and out:
                out.append(" ")
            pending_space = False
            position = index + 1
            out.append("[")
            while position < length:
                out.append(sql[position])
                if sql[position] == "]":
                    position += 1
                    break
                position += 1
            index = position
            continue
        append_plain(char)
        index += 1
    return "".join(out).strip()


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    """Split on a delimiter at parenthesis depth zero, outside literals."""

    masked = mask_sql_literals(text)
    pieces: list[str] = []
    start = 0
    depth = 0
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise BusinessError(E_SQL_SYNTAX, "SQL 括号不匹配", expression=text)
        elif depth == 0 and masked.startswith(delimiter, index):
            pieces.append(text[start:index].strip())
            index += len(delimiter)
            start = index
            continue
        index += 1
    if depth != 0:
        raise BusinessError(E_SQL_SYNTAX, "SQL 括号不匹配", expression=text)
    pieces.append(text[start:].strip())
    return pieces


def top_level_words(text: str) -> list[tuple[str, int, int]]:
    """Return word tokens at paren depth zero with their original offsets."""

    masked = mask_sql_literals(text)
    words: list[tuple[str, int, int]] = []
    depth = 0
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            index += 1
            continue
        if depth == 0 and (char.isalpha() or char == "_"):
            start = index
            index += 1
            while index < len(masked) and (masked[index].isalnum() or masked[index] in "_$"):
                index += 1
            words.append((text[start:index].upper(), start, index))
            continue
        index += 1
    return words


def split_by_top_level_keyword(text: str, keyword: str) -> list[str]:
    """Split on a whole keyword (for example AND) at depth zero."""

    masked = mask_sql_literals(text)
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", re.IGNORECASE)
    depth = 0
    pieces: list[str] = []
    start = 0
    index = 0
    while index < len(masked):
        char = masked[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0:
            match = pattern.match(masked, index)
            if match:
                pieces.append(text[start:index].strip())
                index = match.end()
                start = index
                continue
        index += 1
    pieces.append(text[start:].strip())
    return [piece for piece in pieces if piece]


def dedupe_preserving_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
