"""Local job registry used for duplicate job_id detection and resume routing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .serialization import canonical_dumps


class JobRegistry:
    def __init__(self, runs_root: str | Path):
        self.root = Path(runs_root)
        self.path = self.root / "index.json"

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema": "numeric-workbench/job-registry/v1", "jobs": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                "E_REGISTRY_CORRUPT",
                "job registry cannot be decoded",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc
        if payload.get("schema") != "numeric-workbench/job-registry/v1":
            raise WorkbenchError(
                "E_REGISTRY_CORRUPT",
                "unsupported job registry schema",
                {"path": str(self.path)},
            )
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(canonical_dumps(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def register(
        self,
        job_id: str,
        *,
        input_sha256: str,
        config_sha256: str,
    ) -> dict[str, Any]:
        payload = self._read()
        if job_id in payload["jobs"]:
            raise WorkbenchError(
                "E_DUPLICATE_JOB_ID",
                "job_id has already been registered",
                {"job_id": job_id},
            )
        record = {
            "job_id": job_id,
            "input_sha256": input_sha256,
            "config_sha256": config_sha256,
            "status": "RUNNING",
            "report_path": None,
        }
        payload["jobs"][job_id] = record
        self._write(payload)
        return record

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        payload = self._read()
        if job_id not in payload["jobs"]:
            raise WorkbenchError(
                "E_JOB_NOT_FOUND",
                "job_id is not registered",
                {"job_id": job_id},
            )
        payload["jobs"][job_id].update(changes)
        self._write(payload)
        return payload["jobs"][job_id]

    def get(self, job_id: str) -> dict[str, Any]:
        payload = self._read()
        if job_id not in payload["jobs"]:
            raise WorkbenchError(
                "E_JOB_NOT_FOUND",
                "job_id is not registered",
                {"job_id": job_id},
            )
        return payload["jobs"][job_id]
