from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import DiagnosticError, ErrorCode
from ..jsonutil import load_json_file
from ..model import ReadResult
from .v1 import read_v1
from .v2 import read_v2
from .v3 import read_v3


@dataclass
class SuiteSources:
    v1: str | Path
    v2: str | Path
    v3: str | Path

    @classmethod
    def from_root(cls, root: str | Path) -> "SuiteSources":
        base = Path(root)
        return cls(
            v1=base / "v1" / "tasks.json",
            v2=base / "v2",
            v3=base / "v3" / "tasks.json",
        )


@dataclass
class SuiteReadResult:
    per_format: dict[str, ReadResult] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tasks(self) -> list[Any]:
        result = []
        for format_name in sorted(self.per_format):
            result.extend(self.per_format[format_name].tasks)
        return result


def dispatch_document(source: str | Path) -> ReadResult:
    path = Path(source)
    if path.is_dir() or path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        return read_v2(path)
    if path.suffix.lower() == ".jsonl":
        return read_v1(path)
    document = load_json_file(path)
    root = document.data
    if not isinstance(root, dict):
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "document root must be an object", field="$", line=1)
    line = document.key_line("version") if "version" in root else 1
    version = root.get("version")
    if version in {1, "1", "v1"}:
        return read_v1(path)
    if version in {3, "3", "v3"}:
        return read_v3(path)
    if version is not None:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported top-level version {version!r}",
            field="version",
            line=line,
            details={"supported": [1, 2, 3]},
        )
    if root.get("format") in {"task-v1", "task-v1-jsonl"}:
        return read_v1(path)
    if root.get("format") == "task-v3":
        return read_v3(path)
    tasks = root.get("tasks")
    if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
        first = tasks[0]
        if "effective_after_s" in first:
            return read_v1(path)
        if "case" in first and "sampling" in first:
            return read_v3(path)
    raise DiagnosticError(
        ErrorCode.E_VERSION_UNSUPPORTED,
        "cannot infer task format from version header or field set",
        field="version",
        line=1,
    )


def read_suite(
    root: str | Path | None = None,
    *,
    sources: SuiteSources | None = None,
) -> SuiteReadResult:
    if sources is None:
        if root is None:
            raise DiagnosticError(ErrorCode.E_IO, "suite root or explicit sources are required")
        sources = SuiteSources.from_root(root)
    result = SuiteReadResult()
    for name, source in (("v1", sources.v1), ("v2", sources.v2), ("v3", sources.v3)):
        if name == "v1":
            read_result = read_v1(source)
        elif name == "v2":
            read_result = read_v2(source)
        else:
            read_result = read_v3(source)
        result.per_format[name] = read_result
        for record in read_result.records:
            result.records.append({"format": name, **record})
    return result

