"""Atomic, versioned five-stage snapshot storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, PipelineError
from .util import checksum_object, read_json, write_json_atomic


STAGES = (
    "validate_normalize",
    "extract_candidates",
    "score_candidates",
    "disambiguate_terms",
    "export_index",
)


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage_index: int) -> Path:
        if stage_index < 0 or stage_index >= len(STAGES):
            raise ValueError("invalid stage index")
        return self.directory / f"{stage_index + 1:02d}_{STAGES[stage_index]}.json"

    def load_all(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        previous_version = 0
        for stage_index, stage_name in enumerate(STAGES):
            path = self.path_for(stage_index)
            if not path.exists():
                break
            try:
                raw = read_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot file cannot be decoded",
                    {"path": path.name, "reason": str(exc)},
                )
            if not isinstance(raw, dict):
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot root must be an object",
                    {"path": path.name},
                )
            required = {
                "stage",
                "stage_index",
                "version",
                "completed_docs",
                "payload",
                "base_fingerprint",
                "checksum",
            }
            missing = sorted(required - set(raw))
            if missing:
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot is missing required fields",
                    {"path": path.name, "missing": missing},
                )
            if raw["stage"] != stage_name or raw["stage_index"] != stage_index:
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot stage identity does not match its filename",
                    {
                        "path": path.name,
                        "expected_stage": stage_name,
                        "actual_stage": raw["stage"],
                    },
                )
            checksum = raw.get("checksum")
            core = {key: value for key, value in raw.items() if key != "checksum"}
            if checksum != checksum_object(core):
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot content checksum does not match",
                    {
                        "path": path.name,
                        "recorded": checksum,
                        "actual": checksum_object(core),
                    },
                )
            version = raw.get("version")
            if (
                not isinstance(version, int)
                or isinstance(version, bool)
                or version <= previous_version
            ):
                raise PipelineError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot versions are not strictly increasing",
                    {
                        "path": path.name,
                        "version": version,
                        "previous_version": previous_version,
                    },
                )
            previous_version = version
            snapshots.append(raw)

        for stage_index in range(len(snapshots), len(STAGES)):
            path = self.path_for(stage_index)
            if path.exists():
                raise PipelineError(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot stage sequence contains a gap",
                    {"path": path.name, "expected_prefix_length": len(snapshots)},
                )
        return snapshots

    def truncated(self, keep_count: int) -> None:
        if keep_count < 0 or keep_count > len(STAGES):
            raise ValueError("invalid snapshot prefix length")
        for stage_index in range(keep_count, len(STAGES)):
            path = self.path_for(stage_index)
            if path.exists():
                path.unlink()

    def clear_all(self) -> None:
        self.truncated(0)

    def commit(
        self,
        stage_index: int,
        payload: Any,
        completed_docs: list[str],
        *,
        base_fingerprint: str,
        rule_fingerprint: str | None = None,
        invalidation: dict[str, Any] | None = None,
        expected_doc_ids: list[str] | None = None,
        forced_version: int | None = None,
    ) -> dict[str, Any]:
        if stage_index < 0 or stage_index >= len(STAGES):
            raise ValueError("invalid stage index")
        existing = self.load_all()
        completed = sorted(completed_docs)
        if len(completed) != len(set(completed)):
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "completed document list contains duplicates",
                {"stage": STAGES[stage_index]},
            )
        if expected_doc_ids is not None and completed != sorted(expected_doc_ids):
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "stage cannot commit before every document is complete",
                {
                    "stage": STAGES[stage_index],
                    "expected_doc_count": len(set(expected_doc_ids)),
                    "completed_doc_count": len(completed),
                },
            )
        if stage_index > len(existing):
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "cannot skip an earlier pipeline stage",
                {
                    "stage": STAGES[stage_index],
                    "committed_prefix_length": len(existing),
                },
            )
        if stage_index < len(existing) - 1:
            raise PipelineError(
                E_SNAPSHOT_ROLLBACK,
                "cannot replace an earlier stage while later stages are committed",
                {
                    "stage": STAGES[stage_index],
                    "committed_prefix_length": len(existing),
                },
            )
        previous_version = existing[-1]["version"] if existing else 0
        version = (
            forced_version
            if forced_version is not None
            else previous_version + 1
        )
        if version <= previous_version:
            raise PipelineError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot commit would roll the version backwards",
                {
                    "stage": STAGES[stage_index],
                    "attempted_version": version,
                    "last_committed_version": previous_version,
                },
            )
        core = {
            "stage": STAGES[stage_index],
            "stage_index": stage_index,
            "version": version,
            "completed_docs": completed,
            "payload": payload,
            "base_fingerprint": base_fingerprint,
            "rule_fingerprint": rule_fingerprint,
            "invalidation": invalidation or {},
        }
        snapshot = dict(core)
        snapshot["checksum"] = checksum_object(core)
        write_json_atomic(self.path_for(stage_index), snapshot)
        return snapshot


def snapshot_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": snapshot["stage"],
        "stage_index": snapshot["stage_index"],
        "version": snapshot["version"],
        "completed_doc_count": len(snapshot["completed_docs"]),
        "checksum": snapshot["checksum"],
        "rule_fingerprint": snapshot.get("rule_fingerprint"),
    }

