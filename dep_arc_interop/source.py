from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

SourceLike: TypeAlias = str | Path


@dataclass(frozen=True)
class SourceText:
    text: str
    name: str
    path: Path | None = None


def read_source(source: SourceLike) -> SourceText:
    if isinstance(source, Path):
        return SourceText(
            text=source.read_text(encoding="utf-8"),
            name=str(source),
            path=source,
        )
    candidate: Path | None = None
    if "\n" not in source and "\r" not in source:
        try:
            candidate = Path(source)
        except (OSError, ValueError):
            candidate = None
    if candidate is not None:
        try:
            exists = candidate.exists()
        except OSError:
            exists = False
        if exists:
            return SourceText(
                text=candidate.read_text(encoding="utf-8"),
                name=str(candidate),
                path=candidate,
            )
    return SourceText(text=source, name="<memory>", path=None)
