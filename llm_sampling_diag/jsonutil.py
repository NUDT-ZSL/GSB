from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DiagnosticError, ErrorCode


@dataclass
class JsonDocument:
    path: Path | None
    text: str
    data: Any
    lines: list[str]

    def key_line(self, key: str, value: Any = None, start_line: int = 1, occurrence: int = 0) -> int:
        return find_key_line(self.lines, key, value=value, start_line=start_line, occurrence=occurrence)


def load_json_file(path: str | Path) -> JsonDocument:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiagnosticError(
            ErrorCode.E_IO,
            f"cannot read input file: {exc}",
            details={"path": str(source)},
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"invalid JSON: {exc.msg}",
            line=exc.lineno,
            field="<json>",
            details={"path": str(source), "column": exc.colno},
        ) from exc
    return JsonDocument(path=source, text=text, data=data, lines=text.splitlines())


def parse_json_text(text: str, source: str = "<memory>") -> JsonDocument:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"invalid JSON: {exc.msg}",
            line=exc.lineno,
            field="<json>",
            details={"path": source, "column": exc.colno},
        ) from exc
    return JsonDocument(path=None, text=text, data=data, lines=text.splitlines())


def find_key_line(
    lines: list[str],
    key: str,
    value: Any = None,
    start_line: int = 1,
    occurrence: int = 0,
) -> int:
    needle = f'"{key}"'
    found: list[int] = []
    for index in range(max(start_line - 1, 0), len(lines)):
        line = lines[index]
        if needle not in line:
            continue
        if value is not None:
            rendered = json.dumps(value, ensure_ascii=False)
            joined = "\n".join(lines[index : min(index + 3, len(lines))])
            if rendered not in joined:
                continue
        found.append(index + 1)
    if not found:
        return max(start_line, 1)
    return found[min(occurrence, len(found) - 1)]


def require_mapping(value: Any, field: str, line: int, case_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an object",
            field=field,
            line=line,
            case_id=case_id,
        )
    return value


def require_list(value: Any, field: str, line: int, case_id: str | None = None) -> list[Any]:
    if not isinstance(value, list):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an array",
            field=field,
            line=line,
            case_id=case_id,
        )
    return value


def require_field(mapping: dict[str, Any], key: str, field: str, line: int, case_id: str | None = None) -> Any:
    if key not in mapping:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"missing required field {field}",
            field=field,
            line=line,
            case_id=case_id,
        )
    return mapping[key]


def parse_int(value: Any, field: str, line: int, case_id: str | None = None, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an integer",
            field=field,
            line=line,
            case_id=case_id,
        )
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be a finite integer",
            field=field,
            line=line,
            case_id=case_id,
        )
    result = int(number)
    if minimum is not None and result < minimum:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be >= {minimum}",
            field=field,
            line=line,
            case_id=case_id,
        )
    return result


def parse_float(value: Any, field: str, line: int, case_id: str | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be a number",
            field=field,
            line=line,
            case_id=case_id,
        )
    number = float(value)
    if not math.isfinite(number):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be finite",
            field=field,
            line=line,
            case_id=case_id,
        )
    return number


def parse_logits(value: Any, field: str, line: int, case_id: str | None = None) -> tuple[float, ...]:
    values = require_list(value, field, line, case_id)
    if not values:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must contain at least one value",
            field=field,
            line=line,
            case_id=case_id,
        )
    result: list[float] = []
    for index, item in enumerate(values):
        number = parse_float(item, f"{field}[{index}]", line, case_id)
        result.append(number)
    return tuple(result)


def parse_shape(value: Any, field: str, line: int, case_id: str | None = None) -> tuple[int, ...]:
    values = require_list(value, field, line, case_id)
    result: list[int] = []
    for index, item in enumerate(values):
        result.append(parse_int(item, f"{field}[{index}]", line, case_id, minimum=0))
    return tuple(result)


def check_version(document: JsonDocument, supported: set[int], *, field: str = "version") -> int:
    root = document.data
    if not isinstance(root, dict):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "document root must be an object",
            field="$",
            line=1,
        )
    if field not in root:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"missing version header {field}",
            field=field,
            line=1,
        )
    line = document.key_line(field, start_line=1)
    raw = root[field]
    if isinstance(raw, str):
        digits = raw.lower().replace("v", "").strip()
        try:
            version = int(digits)
        except ValueError as exc:
            raise DiagnosticError(
                ErrorCode.E_VERSION_UNSUPPORTED,
                f"unsupported version {raw!r}",
                field=field,
                line=line,
            ) from exc
    elif isinstance(raw, int) and not isinstance(raw, bool):
        version = raw
    else:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"{field} must be an integer or version string",
            field=field,
            line=line,
        )
    if version not in supported:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported version {version}",
            field=field,
            line=line,
            details={"supported": sorted(supported)},
        )
    return version

