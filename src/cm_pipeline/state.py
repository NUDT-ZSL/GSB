from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json_bytes
from .errors import PipelineError

STATE_SCHEMA = "cm-state-v1"
SNAPSHOT_SCHEMA = "cm-snapshot-v1"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class StateStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = self.root / "snapshots"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / ".commit.lock"

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "current_version": 0,
            "current_snapshot_id": None,
            "versions": [],
        }

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(
                "E_INCONSISTENT",
                f"Cannot read state file: {exc}",
                stage="state",
                field="state",
            ) from exc
        if state.get("schema") != STATE_SCHEMA:
            raise PipelineError(
                "E_INCONSISTENT",
                "Unsupported state schema",
                stage="state",
                field="schema",
            )
        return state

    def current_version(self) -> int:
        return int(self.read_state()["current_version"])

    def _snapshot_path(self, version: int) -> Path:
        return self.snapshots / f"{int(version):06d}.json"

    def load_snapshot(self, version: int | None = None) -> dict[str, Any] | None:
        state = self.read_state()
        if version is None:
            version = int(state["current_version"])
        if int(version) <= 0:
            return None
        path = self._snapshot_path(int(version))
        if not path.exists():
            raise PipelineError(
                "E_LINEAGE_BROKEN",
                f"Snapshot version {version} is missing",
                stage="state",
                field="snapshot",
                details={"version": int(version)},
            )
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(
                "E_INCONSISTENT",
                f"Cannot read snapshot {version}: {exc}",
                stage="state",
                field="snapshot",
            ) from exc
        if snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise PipelineError(
                "E_INCONSISTENT",
                "Unsupported snapshot schema",
                stage="state",
                field="schema",
            )
        return snapshot

    def commit_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        expected_version: int,
    ) -> None:
        state = self.read_state()
        current = int(state["current_version"])
        version = int(snapshot["version"])
        if int(expected_version) != current:
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                f"Expected base version {expected_version}, current is {current}",
                stage="commit",
                field="version",
                details={"expected": int(expected_version), "current": current},
            )
        if version <= current:
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                f"Cannot commit version {version} over completed version {current}",
                stage="commit",
                field="version",
                details={"target": version, "current": current},
            )
        path = self._snapshot_path(version)
        if path.exists():
            raise PipelineError(
                "E_SNAPSHOT_ROLLBACK",
                f"Snapshot version {version} already exists and is immutable",
                stage="commit",
                field="version",
                details={"version": version},
            )
        _atomic_write(path, canonical_json_bytes(snapshot))
        versions = sorted({int(item) for item in state.get("versions", [])} | {version})
        new_state = {
            "schema": STATE_SCHEMA,
            "current_version": version,
            "current_snapshot_id": snapshot["snapshot_id"],
            "versions": versions,
        }
        _atomic_write(self.state_path, canonical_json_bytes(new_state))

    @contextmanager
    def commit_lock(self) -> Iterator[None]:
        try:
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise PipelineError(
                "E_STAGE_BUSY",
                "Another incremental commit currently holds the commit stage",
                stage="commit",
                field="lock",
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

