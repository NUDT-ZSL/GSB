"""Versioned, checksummed, whole-snapshot replacement."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .errors import WorkbenchError, snapshot_corrupt, snapshot_rollback
from .serialization import canonical_dumps, sha256_bytes, sha256_json

SNAPSHOT_STAGES = (
    "normalization_statistics",
    "chunk_plan",
    "precision_error_spectra",
    "trajectory_summaries",
)


class SnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, job_id: str) -> Path:
        return self.root / job_id / "snapshot.json"

    @staticmethod
    def _checksum(stage: str, version: int, payload: Any) -> str:
        return sha256_json(
            {"stage": stage, "version": int(version), "payload": payload}
        )

    def commit(
        self,
        job_id: str,
        *,
        stage_payloads: Mapping[str, Any],
        input_sha256: str,
        config_sha256: str,
        versions: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        missing = [stage for stage in SNAPSHOT_STAGES if stage not in stage_payloads]
        if missing:
            raise ValueError(f"missing snapshot stages: {missing}")
        extra = [stage for stage in stage_payloads if stage not in SNAPSHOT_STAGES]
        if extra:
            raise ValueError(f"unknown snapshot stages: {extra}")

        target = self.path(job_id)
        previous_state: dict[str, Any] | None = None
        if target.is_file():
            previous_state = self.load(job_id)

        previous_stages = previous_state["stages"] if previous_state else {}
        committed: dict[str, Any] = {}
        replaced: list[dict[str, Any]] = []
        unchanged: list[str] = []

        for stage in SNAPSHOT_STAGES:
            payload = stage_payloads[stage]
            previous_entry = previous_stages.get(stage)
            previous_version = int(previous_entry["version"]) if previous_entry else 0
            previous_checksum = (
                previous_entry["checksum"] if previous_entry else None
            )
            if versions is not None and stage in versions:
                version = int(versions[stage])
            else:
                version = previous_version + 1 if previous_version else 1
            if version < previous_version:
                raise snapshot_rollback(stage, version, previous_version)
            checksum = self._checksum(stage, version, payload)
            reason = "new"
            if previous_entry:
                if checksum == previous_checksum and version == previous_version:
                    reason = "unchanged"
                    unchanged.append(stage)
                elif version == previous_version:
                    reason = "checksum_change_same_version"
                elif checksum == previous_checksum:
                    reason = "version_change_same_content"
                else:
                    reason = "version_and_checksum_change"
                if reason != "unchanged":
                    replaced.append(
                        {
                            "stage": stage,
                            "previous_version": previous_version,
                            "new_version": version,
                            "previous_checksum": previous_checksum,
                            "new_checksum": checksum,
                            "reason": reason,
                        }
                    )
            committed[stage] = {
                "version": version,
                "checksum": checksum,
                "payload": payload,
            }

        state = {
            "schema": "numeric-workbench/snapshot/v1",
            "job_id": job_id,
            "input_sha256": input_sha256,
            "config_sha256": config_sha256,
            "replacement_mode": "whole_snapshot_replace",
            "stages": committed,
        }
        payload = canonical_dumps(state) + "\n"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return {
            "path": str(target),
            "snapshot_sha256": sha256_bytes(payload.encode("utf-8")),
            "replacement_mode": "whole_snapshot_replace",
            "versions": {
                stage: committed[stage]["version"] for stage in SNAPSHOT_STAGES
            },
            "checksums": {
                stage: committed[stage]["checksum"] for stage in SNAPSHOT_STAGES
            },
            "replaced_stages": replaced,
            "unchanged_stages": unchanged,
        }

    def load(
        self,
        job_id: str,
        *,
        expected: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        target = self.path(job_id)
        if not target.is_file():
            raise WorkbenchError(
                "E_SNAPSHOT_MISSING",
                "snapshot does not exist",
                {"job_id": job_id, "path": str(target)},
            )
        try:
            state = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot cannot be decoded",
                {"stage": "$root", "version": None, "reason": str(exc)},
            ) from exc
        if state.get("schema") != "numeric-workbench/snapshot/v1":
            raise WorkbenchError(
                "E_SNAPSHOT_CORRUPT",
                "unsupported snapshot schema",
                {"stage": "$root", "version": None, "schema": state.get("schema")},
            )
        stages = state.get("stages")
        if not isinstance(stages, dict):
            raise WorkbenchError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot stages are missing",
                {"stage": "$root", "version": None},
            )
        changes: list[dict[str, Any]] = []
        for stage in SNAPSHOT_STAGES:
            entry = stages.get(stage)
            if not isinstance(entry, dict):
                raise snapshot_corrupt(stage, 0, "snapshot stage is missing")
            version = int(entry.get("version", 0))
            expected_checksum = self._checksum(stage, version, entry.get("payload"))
            if entry.get("checksum") != expected_checksum:
                raise snapshot_corrupt(
                    stage,
                    version,
                    "snapshot content checksum does not match its payload",
                )
            if expected is not None and stage in expected:
                wanted_version = int(expected[stage].get("version", version))
                wanted_checksum = expected[stage].get("checksum")
                version_changed = wanted_version != version
                checksum_changed = (
                    wanted_checksum is not None and wanted_checksum != expected_checksum
                )
                if version_changed or checksum_changed:
                    changes.append(
                        {
                            "stage": stage,
                            "version": version,
                            "checksum": expected_checksum,
                            "expected_version": wanted_version,
                            "expected_checksum": wanted_checksum,
                            "action": "whole_stage_replace",
                        }
                    )
        state["load_changes"] = changes
        return state

    def manifest(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "versions": {
                stage: state["stages"][stage]["version"]
                for stage in SNAPSHOT_STAGES
            },
            "checksums": {
                stage: state["stages"][stage]["checksum"]
                for stage in SNAPSHOT_STAGES
            },
            "replacement_mode": state.get("replacement_mode"),
        }
