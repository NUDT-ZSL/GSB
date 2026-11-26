from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DiagnosticError
from .utils import read_json, sha256_json, write_json_atomic


@dataclass(frozen=True)
class Checkpoint:
    version: int
    path: str
    checksum: str
    stage: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": self.path,
            "checksum": self.checksum,
            "stage": self.stage,
            "payload": self.payload,
        }


def _checkpoint_document(
    version: int, stage: str, payload: dict[str, Any]
) -> dict[str, Any]:
    content = {
        "version": version,
        "stage": stage,
        "payload": payload,
    }
    return {**content, "checksum": sha256_json(content)}


class CheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self) -> list[Path]:
        return sorted(self.root.glob("cp-*.json"))

    def latest_version(self) -> int:
        versions = []
        for path in self._paths():
            try:
                versions.append(int(path.stem.split("-")[-1]))
            except ValueError:
                continue
        return max(versions, default=0)

    def save(self, stage: str, payload: dict[str, Any]) -> Checkpoint:
        version = self.latest_version() + 1
        document = _checkpoint_document(version, stage, payload)
        path = self.root / f"cp-{version:08d}.json"
        write_json_atomic(path, document)
        return Checkpoint(
            version=version,
            path=str(path),
            checksum=document["checksum"],
            stage=stage,
            payload=payload,
        )

    def load_path(self, path: str | Path) -> Checkpoint:
        checkpoint_path = Path(path)
        try:
            document = read_json(checkpoint_path)
        except Exception as exc:
            raise DiagnosticError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint",
                f"cannot parse checkpoint {checkpoint_path}: {exc}",
            ) from exc
        content = {
            "version": document.get("version"),
            "stage": document.get("stage"),
            "payload": document.get("payload"),
        }
        checksum = sha256_json(content)
        if checksum != document.get("checksum"):
            raise DiagnosticError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint",
                "checkpoint content checksum mismatch",
                details={"path": str(checkpoint_path)},
            )
        return Checkpoint(
            version=int(document["version"]),
            path=str(checkpoint_path),
            checksum=str(document["checksum"]),
            stage=str(document["stage"]),
            payload=dict(document["payload"]),
        )

    def latest(self, *, expected: dict[str, Any] | None = None) -> Checkpoint | None:
        failures: list[dict[str, Any]] = []
        identity_mismatch = False
        for path in reversed(self._paths()):
            try:
                checkpoint = self.load_path(path)
            except DiagnosticError as exc:
                failures.append({"path": str(path), "error": exc.to_dict()})
                continue
            if expected is not None:
                mismatch = {
                    key: {
                        "expected": value,
                        "actual": checkpoint.payload.get(key),
                    }
                    for key, value in expected.items()
                    if checkpoint.payload.get(key) != value
                }
                if mismatch:
                    identity_mismatch = True
                    failures.append(
                        {
                            "path": str(path),
                            "error": {
                                "code": "E_CHECKPOINT_IDENTITY_MISMATCH",
                                "details": mismatch,
                            },
                        }
                    )
                    continue
            return checkpoint
        if failures:
            error_code = (
                "E_CHECKPOINT_IDENTITY_MISMATCH"
                if identity_mismatch
                else "E_CHECKPOINT_CORRUPT"
            )
            raise DiagnosticError(
                error_code,
                "checkpoint",
                "no valid committed checkpoint could be loaded",
                details={"failures": failures},
            )
        return None

    def metadata(self) -> dict[str, Any]:
        paths = self._paths()
        return {
            "directory": str(self.root),
            "checkpoint_count": len(paths),
            "latest_version": self.latest_version(),
            "files": [path.name for path in paths],
        }


class ForcedTermination(RuntimeError):
    def __init__(self, *, stage: str, committed_steps: int, checkpoint: str) -> None:
        super().__init__(
            f"forced termination at {stage} after {committed_steps} committed steps"
        )
        self.stage = stage
        self.committed_steps = committed_steps
        self.checkpoint = checkpoint

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "E_FORCED_TERMINATION",
            "stage": self.stage,
            "committed_steps": self.committed_steps,
            "checkpoint": self.checkpoint,
        }

