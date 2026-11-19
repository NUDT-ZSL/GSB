from __future__ import annotations

import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import atomic_write_text, canonical_json, checksum, read_json
from .errors import DomainError, ErrorCode

COMPONENT_NAMES = ("plan", "budget_ledger", "result_summary")


class SnapshotStore:
    """Whole-file versioned snapshots with a non-blocking commit lock."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.root / "stage_snapshot.json"
        self.lock_path = self.root / ".stage.lock"

    @contextmanager
    def stage_lock(self) -> Iterator[None]:
        try:
            os.mkdir(self.lock_path)
        except FileExistsError as exc:
            raise DomainError(
                ErrorCode.E_STAGE_BUSY,
                "another plan submission currently owns the stage lock",
                detail={"lock_path": str(self.lock_path)},
            ) from exc
        try:
            (self.lock_path / "owner").write_text(
                canonical_json({"pid": os.getpid()}), encoding="utf-8"
            )
            yield
        finally:
            try:
                (self.lock_path / "owner").unlink(missing_ok=True)
                self.lock_path.rmdir()
            except OSError:
                pass

    def _verify_snapshot(self, raw: Any) -> dict[str, Any]:
        try:
            if not isinstance(raw, dict) or raw.get("format") != "qconv-snapshot-v1":
                raise ValueError("unknown snapshot format")
            generation = raw["generation"]
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise ValueError("generation must be a positive integer")
            components = raw["components"]
            if not isinstance(components, dict) or set(components) != set(COMPONENT_NAMES):
                raise ValueError("snapshot component set is invalid")
            for name in COMPONENT_NAMES:
                wrapper = components[name]
                if not isinstance(wrapper, dict):
                    raise ValueError(f"component {name} is not an object")
                if wrapper.get("version") != generation:
                    raise ValueError(f"component {name} version is not current")
                payload = wrapper["payload"]
                expected = checksum(payload)
                if wrapper.get("checksum") != expected:
                    raise ValueError(f"component {name} checksum mismatch")
            body = {
                "format": raw["format"],
                "generation": generation,
                "previous_generation": raw.get("previous_generation"),
                "components": components,
            }
            expected_snapshot = checksum(body)
            if raw.get("snapshot_checksum") != expected_snapshot:
                raise ValueError("snapshot checksum mismatch")
            return raw
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                f"snapshot failed integrity verification: {exc}",
                detail={"path": str(self.snapshot_path)},
            ) from exc

    def load(self) -> dict[str, Any] | None:
        if not self.snapshot_path.exists():
            return None
        try:
            raw = read_json(self.snapshot_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                f"snapshot cannot be decoded: {exc}",
                detail={"path": str(self.snapshot_path)},
            ) from exc
        return self._verify_snapshot(raw)

    def commit(self, components: dict[str, Any]) -> dict[str, Any]:
        if set(components) != set(COMPONENT_NAMES):
            raise DomainError(
                ErrorCode.E_INCONSISTENT,
                f"snapshot requires components {list(COMPONENT_NAMES)}",
                detail={"provided": sorted(components)},
            )
        with self.stage_lock():
            current = self.load()
            previous_generation = int(current["generation"]) if current else 0
            generation = previous_generation + 1
            wrappers = {
                name: {
                    "version": generation,
                    "checksum": checksum(components[name]),
                    "payload": components[name],
                }
                for name in COMPONENT_NAMES
            }
            body = {
                "format": "qconv-snapshot-v1",
                "generation": generation,
                "previous_generation": previous_generation,
                "components": wrappers,
            }
            snapshot = body | {"snapshot_checksum": checksum(body)}
            atomic_write_text(
                self.snapshot_path,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
            )
            return snapshot

    def clean(self) -> dict[str, Any]:
        removed: list[str] = []
        if self.snapshot_path.exists():
            self.snapshot_path.unlink()
            removed.append(str(self.snapshot_path))
        for path in self.root.glob(".stage_snapshot.json.*.tmp"):
            path.unlink(missing_ok=True)
            removed.append(str(path))
        if self.lock_path.exists():
            shutil.rmtree(self.lock_path)
            removed.append(str(self.lock_path))
        return {"removed": removed, "root": str(self.root)}

