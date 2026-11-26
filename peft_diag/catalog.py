"""Snapshot catalog access and atomic updates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes, content_hash, read_json
from .errors import format_error


class Catalog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise format_error(
                "snapshot catalog is missing",
                field="catalog_path",
                details={"path": str(self.path)},
            )
        try:
            data = read_json(self.path)
        except Exception as exc:
            raise format_error(
                f"cannot parse snapshot catalog: {exc}",
                field="catalog_path",
                details={"path": str(self.path)},
            )
        if not isinstance(data, dict) or data.get("catalog_version") != "v1":
            raise format_error(
                "unsupported or malformed snapshot catalog",
                field="catalog_version",
                details={"path": str(self.path)},
            )
        if not isinstance(data.get("tasks"), dict):
            raise format_error("catalog tasks must be an object", field="catalog_path")
        self.data: dict[str, Any] = data
        self.root = self.path.parent
        self.config_generation = int(data.get("config_generation", 1))

    def snapshot_path(self, task_id: str, format_version: str) -> str:
        entry = self.data["tasks"].get(task_id)
        if not isinstance(entry, dict):
            raise format_error(
                "task is absent from snapshot catalog",
                task_id=task_id,
                field="catalog",
            )
        raw_path = entry.get(format_version)
        if not isinstance(raw_path, str):
            raise format_error(
                f"catalog has no {format_version} snapshot for task",
                task_id=task_id,
                field="format_version",
            )
        return str((self.root / raw_path).resolve())

    def entry(self, task_id: str) -> dict[str, Any]:
        entry = self.data["tasks"].get(task_id)
        if not isinstance(entry, dict):
            raise format_error("task is absent from snapshot catalog", task_id=task_id, field="catalog")
        return dict(entry)

    def replace_snapshot(
        self,
        task_id: str,
        *,
        rev: int,
        snapshot_id: str,
        semantic_hash: str,
        format_version: str,
        absolute_path: str,
    ) -> None:
        entry = self.entry(task_id)
        try:
            relative = Path(absolute_path).resolve().relative_to(self.root.resolve())
        except ValueError:
            relative = Path(absolute_path).resolve()
        if format_version in {"v1", "v2"}:
            entry[format_version] = str(relative)
        else:
            raise format_error(
                f"unsupported snapshot format {format_version}",
                task_id=task_id,
                field="format_version",
            )
        entry["revision"] = rev
        entry["snapshot_id"] = snapshot_id
        entry["semantic_hash"] = semantic_hash
        self.data["tasks"][task_id] = entry

    def increment_generation(self) -> int:
        self.config_generation += 1
        self.data["config_generation"] = self.config_generation
        return self.config_generation

    def content_hash(self) -> str:
        return content_hash(self.data)

    def save(self) -> str:
        raw = canonical_bytes(self.data)
        atomic_write_bytes(self.path, raw)
        return content_hash(self.data)


__all__ = ["Catalog"]

