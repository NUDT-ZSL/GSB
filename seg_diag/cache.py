"""Small deterministic content-addressed JSON cache for ledger rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import canonical_bytes


class RowCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def path_for(self, key: str) -> Path:
        prefix = key[:2]
        directory = self.root / prefix
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        with path.open("r", encoding="utf-8") as handle:
            row = json.load(handle)
        self.hits += 1
        return row

    def put(self, row: dict[str, Any]) -> Path:
        key = row["dependency_key"]
        path = self.path_for(key)
        if not path.exists():
            path.write_bytes(canonical_bytes(row) + b"\n")
        return path

    def load_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for key in keys:
            row = self.get(key)
            if row is not None:
                rows[row["row_id"]] = row
        return rows
