"""Versioned, immutable checkpoint chain with an atomic latest pointer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes, content_hash, read_json
from .errors import corrupt, format_error


COMMIT_PATTERN = re.compile(r"^v(\d{8})\.json$")


class CheckpointManager:
    def __init__(self, state_dir: str | Path, run_id: str) -> None:
        self.state_dir = Path(state_dir)
        self.run_id = run_id
        self.root = self.state_dir / "runs" / run_id
        self.commits = self.root / "commits"
        self.commits.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.root / "latest.json"
        self.recovery_notes: list[dict[str, Any]] = []

    def _commit_path(self, version: int) -> Path:
        return self.commits / f"v{version:08d}.json"

    def list_versions(self) -> list[int]:
        versions: list[int] = []
        for path in self.commits.iterdir():
            match = COMMIT_PATTERN.match(path.name)
            if match:
                versions.append(int(match.group(1)))
        return sorted(versions)

    def _verify_payload(self, data: Any, path: Path) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise corrupt("checkpoint root must be an object", field="checkpoint", details={"path": str(path)})
        checksum = data.get("checksum")
        body = {key: value for key, value in data.items() if key != "checksum"}
        actual = content_hash(body)
        if not isinstance(checksum, str) or actual != checksum:
            raise corrupt(
                "checkpoint checksum mismatch",
                field="checksum",
                details={"path": str(path), "expected": checksum, "actual": actual},
            )
        if data.get("run_id") != self.run_id:
            raise format_error(
                "checkpoint run_id mismatch",
                field="run_id",
                details={"expected": self.run_id, "actual": data.get("run_id"), "path": str(path)},
            )
        if not isinstance(data.get("version"), int) or data["version"] < 1:
            raise corrupt("checkpoint version is invalid", field="version", details={"path": str(path)})
        if not isinstance(data.get("stage"), str):
            raise corrupt("checkpoint stage is invalid", field="stage", details={"path": str(path)})
        if not isinstance(data.get("completed_tasks"), list):
            raise corrupt("checkpoint completed_tasks is invalid", field="completed_tasks", details={"path": str(path)})
        return data

    def load_version(self, version: int) -> dict[str, Any]:
        path = self._commit_path(version)
        if not path.is_file():
            raise corrupt(
                "checkpoint commit is missing",
                field="version",
                details={"version": version, "path": str(path)},
            )
        try:
            data = read_json(path)
        except Exception as exc:
            raise corrupt(
                f"checkpoint commit is unreadable: {exc}",
                field="checkpoint",
                details={"version": version, "path": str(path)},
            )
        data = self._verify_payload(data, path)
        if int(data["version"]) != version:
            raise corrupt(
                "checkpoint filename and version disagree",
                field="version",
                details={"filename": version, "body": data["version"]},
            )
        return data

    def recover_latest(self) -> dict[str, Any] | None:
        self.recovery_notes = []
        versions = self.list_versions()
        if not versions:
            return None
        latest_pointer: dict[str, Any] | None = None
        if self.latest_path.is_file():
            try:
                latest_pointer = read_json(self.latest_path)
            except Exception as exc:
                self.recovery_notes.append(
                    {"action": "ignore_latest_pointer", "reason": f"unreadable: {exc}"}
                )
        for version in sorted(versions, reverse=True):
            try:
                data = self.load_version(version)
                if latest_pointer and int(latest_pointer.get("version", -1)) < version:
                    self.recovery_notes.append(
                        {
                            "action": "recover_newer_commit",
                            "version": version,
                            "pointer_version": latest_pointer.get("version"),
                        }
                    )
                return data
            except Exception as exc:
                self.recovery_notes.append(
                    {
                        "action": "skip_invalid_commit",
                        "version": version,
                        "reason": getattr(exc, "message", str(exc)),
                    }
                )
        return None

    def next_version(self) -> int:
        versions = self.list_versions()
        return (versions[-1] + 1) if versions else 1

    def write_commit(self, body: dict[str, Any]) -> dict[str, Any]:
        if "checksum" in body:
            raise ValueError("caller must not provide checksum")
        version = self.next_version()
        body = dict(body)
        body["run_id"] = self.run_id
        body["version"] = version
        checksum = content_hash(body)
        stored = dict(body)
        stored["checksum"] = checksum
        path = self._commit_path(version)
        atomic_write_bytes(path, canonical_bytes(stored))
        pointer = {
            "run_id": self.run_id,
            "version": version,
            "checksum": checksum,
            "path": path.name,
        }
        atomic_write_bytes(self.latest_path, canonical_bytes(pointer))
        return stored

    def summary(self) -> dict[str, Any]:
        versions = self.list_versions()
        latest: dict[str, Any] | None = None
        for version in reversed(versions):
            try:
                data = self.load_version(version)
                latest = {
                    "version": data["version"],
                    "stage": data["stage"],
                    "cycle": data.get("cycle", 0),
                    "completed_tasks": data["completed_tasks"],
                    "checksum": data["checksum"],
                    "task_count": len(data.get("task_states", {})),
                }
                break
            except Exception:
                continue
        return {
            "run_id": self.run_id,
            "checkpoint_root": str(self.root),
            "commit_versions": versions,
            "latest": latest,
            "recovery_notes": self.recovery_notes,
        }


__all__ = ["CheckpointManager"]

