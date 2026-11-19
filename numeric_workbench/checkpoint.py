"""Atomic round-boundary checkpoints for deterministic resume."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .serialization import canonical_dumps, sha256_bytes


class CheckpointStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, job_id: str) -> Path:
        return self.root / job_id / "checkpoint.json"

    def commit(
        self,
        job_id: str,
        *,
        input_sha256: str,
        config_sha256: str,
        state: dict[str, Any],
    ) -> str:
        target = self.path(job_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        wrapper = {
            "schema": "numeric-workbench/checkpoint/v1",
            "job_id": job_id,
            "input_sha256": input_sha256,
            "config_sha256": config_sha256,
            "state": state,
        }
        payload = canonical_dumps(wrapper) + "\n"
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return sha256_bytes(payload.encode("utf-8"))

    def load(
        self,
        job_id: str,
        *,
        input_sha256: str,
        config_sha256: str,
        precision: str,
        reduce_mode: str,
        chunk_plan_sha256: str,
        train_index_sha256: str,
    ) -> dict[str, Any]:
        target = self.path(job_id)
        if not target.is_file():
            raise WorkbenchError(
                "E_RESUME_NOT_AVAILABLE",
                "no committed checkpoint exists for this job",
                {"job_id": job_id, "path": str(target)},
            )
        try:
            wrapper = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint cannot be decoded",
                {"job_id": job_id, "reason": str(exc)},
            ) from exc
        if wrapper.get("schema") != "numeric-workbench/checkpoint/v1":
            raise WorkbenchError(
                "E_CHECKPOINT_CORRUPT",
                "unsupported checkpoint schema",
                {"job_id": job_id, "schema": wrapper.get("schema")},
            )
        expected = {
            "job_id": job_id,
            "input_sha256": input_sha256,
            "config_sha256": config_sha256,
        }
        for key, value in expected.items():
            if wrapper.get(key) != value:
                raise WorkbenchError(
                    "E_RESUME_MISMATCH",
                    f"checkpoint {key} does not match the current request",
                    {"field": key, "checkpoint": wrapper.get(key), "current": value},
                )
        state = wrapper.get("state")
        if not isinstance(state, dict):
            raise WorkbenchError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint state is missing",
                {"job_id": job_id},
            )
        checks = {
            "precision": precision,
            "reduce_mode": reduce_mode,
            "chunk_plan_sha256": chunk_plan_sha256,
            "train_index_sha256": train_index_sha256,
        }
        for key, value in checks.items():
            if state.get(key) != value:
                raise WorkbenchError(
                    "E_RESUME_MISMATCH",
                    f"checkpoint {key} does not match the current numerical plan",
                    {
                        "field": key,
                        "checkpoint": state.get(key),
                        "current": value,
                    },
                )
        if int(state.get("rounds_completed", 0)) != len(
            state.get("loss_trajectory_hex", [])
        ):
            raise WorkbenchError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint trajectory length does not match committed rounds",
                {"job_id": job_id},
            )
        if int(state.get("rounds_completed", 0)) != len(
            state.get("parameter_trajectory", [])
        ):
            raise WorkbenchError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint parameter trajectory length does not match committed rounds",
                {"job_id": job_id},
            )
        return state
