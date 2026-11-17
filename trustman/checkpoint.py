"""Checksummed checkpoint envelopes with atomic persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .atomic import (
    atomic_write_json,
    orphan_temp_files,
    read_json_file,
)
from .errors import E_CHECKPOINT_CORRUPT
from .models import (
    CHECKPOINT_SCHEMA,
    CHECKPOINT_STRUCTURE_VERSION,
    canonical_json,
    sha256_value,
)


CHECKPOINT_PATH = Path("state") / "checkpoint.json"


def _checkpoint_path(workspace: str | Path) -> Path:
    return Path(workspace) / CHECKPOINT_PATH


def checkpoint_checksum(payload: dict[str, Any], structure_version: int) -> str:
    return sha256_value(
        {
            "structure_version": structure_version,
            "payload": payload,
        }
    )


@dataclass
class CheckpointLoad:
    status: str
    payload: dict[str, Any] | None = None
    structure_version: int | None = None
    reason: str | None = None
    message: str | None = None
    ignored_temp_files: list[str] = field(default_factory=list)
    path: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class CheckpointStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.path = _checkpoint_path(self.workspace)
        self.directory = self.path.parent

    def load(self) -> CheckpointLoad:
        ignored = orphan_temp_files(self.directory, self.path.name)
        if not self.path.exists():
            return CheckpointLoad(
                status="missing",
                reason="checkpoint_not_found",
                message="no checkpoint exists",
                ignored_temp_files=ignored,
                path=str(self.path),
            )
        try:
            document = read_json_file(self.path)
        except (OSError, UnicodeDecodeError, ValueError):
            return CheckpointLoad(
                status="corrupt",
                reason=E_CHECKPOINT_CORRUPT,
                message="checkpoint is not valid JSON",
                ignored_temp_files=ignored,
                path=str(self.path),
            )
        if not isinstance(document, dict) or set(document) != {
            "schema",
            "structure_version",
            "payload",
            "checksum",
        }:
            return self._corrupt("checkpoint envelope fields are invalid", ignored)
        if document.get("schema") != CHECKPOINT_SCHEMA:
            return self._corrupt("checkpoint schema is invalid", ignored)
        structure_version = document.get("structure_version")
        payload = document.get("payload")
        checksum = document.get("checksum")
        if (
            type(structure_version) is not int
            or not isinstance(payload, dict)
            or not isinstance(checksum, str)
        ):
            return self._corrupt("checkpoint envelope types are invalid", ignored)
        expected = checkpoint_checksum(payload, structure_version)
        if checksum != expected:
            return self._corrupt("checkpoint checksum mismatch", ignored)
        return CheckpointLoad(
            status="ok",
            payload=payload,
            structure_version=structure_version,
            ignored_temp_files=ignored,
            path=str(self.path),
        )

    def _corrupt(
        self, message: str, ignored_temp_files: list[str]
    ) -> CheckpointLoad:
        return CheckpointLoad(
            status="corrupt",
            reason=E_CHECKPOINT_CORRUPT,
            message=message,
            ignored_temp_files=ignored_temp_files,
            path=str(self.path),
        )

    def save(
        self,
        payload: dict[str, Any],
        *,
        structure_version: int = CHECKPOINT_STRUCTURE_VERSION,
    ) -> None:
        envelope = {
            "schema": CHECKPOINT_SCHEMA,
            "structure_version": structure_version,
            "payload": payload,
            "checksum": checkpoint_checksum(payload, structure_version),
        }
        atomic_write_json(self.path, envelope, pretty=False)


def result_signature(result: dict[str, Any]) -> str:
    """Deterministic fields that replay must reproduce exactly."""

    return canonical_json(
        {
            "status": result.get("status"),
            "conclusion": result.get("conclusion"),
            "errors": result.get("errors"),
            "invalidations": result.get("invalidations"),
            "basis": result.get("basis"),
            "actual_summary": result.get("actual_summary"),
            "matches_expected": result.get("matches_expected"),
        }
    )


def replay_mismatches(
    stored_results: dict[str, dict[str, Any]],
    expected_results: dict[str, dict[str, Any]],
) -> list[str]:
    mismatches: list[str] = []
    for chain_id in sorted(stored_results):
        stored = stored_results[chain_id]
        expected = expected_results.get(chain_id)
        if expected is None:
            mismatches.append(chain_id)
            continue
        if stored.get("status") == "success":
            if result_signature(stored) != result_signature(expected):
                mismatches.append(chain_id)
        elif stored.get("status") != expected.get("status"):
            mismatches.append(chain_id)
    return mismatches


def normalized_results(
    results: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        result["chain_id"]: result
        for result in sorted(results, key=lambda item: item["chain_id"])
    }
