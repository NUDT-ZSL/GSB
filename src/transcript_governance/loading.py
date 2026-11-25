"""Format-detecting transcript loader."""

from __future__ import annotations

import json
from pathlib import Path

from .formats_v1 import read_v1
from .formats_v2 import read_v2_dataset
from .results import LoadResult


def load_transcript(path: str | Path) -> LoadResult:
    """Load v1 text or a v2 manifest/shard/dataset directory by path/content."""

    path = Path(path)
    if path.is_dir():
        return read_v2_dataset(path)

    if path.suffix.lower() == ".json":
        return read_v2_dataset(path)

    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0:1]
    except OSError:
        first_line = []

    if first_line:
        stripped = first_line[0].strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and "utterances" in payload:
                return read_v2_dataset(path)

    return read_v1(path)
