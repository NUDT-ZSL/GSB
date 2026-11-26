from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import read_jsonl
from .config import PRECISION_PROFILES, Config, resolve_precision_profile
from .errors import (
    E_DUPLICATE_UTTERANCE_ID,
    E_REFERENCE_MISMATCH,
    E_TREE_VERSION_ROLLBACK,
    DiagnosticError,
)


def load_tasks(sample_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(sample_dir) / "tasks.jsonl"
    tasks = read_jsonl(path)
    validate_task_records(tasks)
    return tasks


def validate_task_records(tasks: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        field_prefix = f"tasks[{index}]"
        utterance_id = task.get("utterance_id")
        if not utterance_id:
            raise DiagnosticError(
                E_DUPLICATE_UTTERANCE_ID,
                "task is missing utterance_id",
                field=f"{field_prefix}.utterance_id",
                details={"index": index},
            )
        if utterance_id in seen:
            raise DiagnosticError(
                E_DUPLICATE_UTTERANCE_ID,
                "duplicate utterance_id in task manifest",
                field="tasks.utterance_id",
                details={"utterance_id": utterance_id, "index": index},
            )
        seen.add(utterance_id)
        profile = task.get("precision_profile", "fp64")
        try:
            resolve_precision_profile(profile)
        except DiagnosticError as exc:
            raise DiagnosticError(
                exc.code,
                exc.message,
                field=f"{field_prefix}.precision_profile",
                details={"value": profile, "known": sorted(PRECISION_PROFILES)},
            ) from exc
        for field_name in ("speaker_id", "text", "start_ms", "end_ms", "decision_tree_rev", "logical_clock_ms"):
            if field_name not in task:
                raise DiagnosticError(
                    "E_FIELD_MISSING",
                    f"task is missing {field_name}",
                    field=f"{field_prefix}.{field_name}",
                )
        if int(task["end_ms"]) <= int(task["start_ms"]):
            raise DiagnosticError(
                "E_INVALID_TIME_RANGE",
                "end_ms must be greater than start_ms",
                field=f"{field_prefix}.end_ms",
                details={"utterance_id": utterance_id},
            )


def validate_revision_watermark(
    tasks: list[dict[str, Any]], previous_watermark: dict[str, Any] | None
) -> None:
    if not previous_watermark:
        return
    utterance_watermarks = previous_watermark.get("utterances")
    if isinstance(utterance_watermarks, dict):
        for task in tasks:
            uid = task["utterance_id"]
            prior = utterance_watermarks.get(uid)
            if not prior:
                continue
            current_rev = int(task.get("decision_tree_rev", 0))
            current_clock = int(task.get("logical_clock_ms", 0))
            if current_rev < int(prior.get("decision_tree_rev", 0)):
                raise DiagnosticError(
                    E_TREE_VERSION_ROLLBACK,
                    "decision_tree_rev is lower than the committed watermark",
                    field="tasks.decision_tree_rev",
                    details={
                        "utterance_id": uid,
                        "current_rev": current_rev,
                        "committed_rev": prior["decision_tree_rev"],
                    },
                )
            if current_clock < int(prior.get("logical_clock_ms", 0)):
                raise DiagnosticError(
                    E_TREE_VERSION_ROLLBACK,
                    "logical_clock_ms is lower than the committed watermark",
                    field="tasks.logical_clock_ms",
                    details={
                        "utterance_id": uid,
                        "current_clock_ms": current_clock,
                        "committed_clock_ms": prior["logical_clock_ms"],
                    },
                )
        return

    previous_rev = int(previous_watermark.get("decision_tree_rev", 0))
    previous_clock = int(previous_watermark.get("logical_clock_ms", 0))
    current_max_rev = max(int(task.get("decision_tree_rev", 0)) for task in tasks)
    current_max_clock = max(int(task.get("logical_clock_ms", 0)) for task in tasks)
    if current_max_rev < previous_rev:
        raise DiagnosticError(
            E_TREE_VERSION_ROLLBACK,
            "decision_tree_rev is lower than the committed watermark",
            field="tasks.decision_tree_rev",
            details={"current_max_rev": current_max_rev, "committed_rev": previous_rev},
        )
    if current_max_clock < previous_clock:
        raise DiagnosticError(
            E_TREE_VERSION_ROLLBACK,
            "logical_clock_ms maximum is lower than the committed watermark",
            field="tasks.logical_clock_ms",
            details={
                "current_max_clock_ms": current_max_clock,
                "committed_clock_ms": previous_clock,
            },
        )


def validate_boundary_references(
    acoustic_records: list[dict[str, Any]],
    boundary_truth: list[dict[str, Any]],
) -> None:
    candidate_keys = {
        (record["utterance_id"], int(candidate["frame_index"]), candidate["boundary_type"])
        for record in acoustic_records
        for candidate in record["candidates"]
    }
    valid_labels = {"B0", "B1", "B2", "B3"}
    for row in boundary_truth:
        key = (row["utterance_id"], int(row["frame_index"]), row["boundary_type"])
        if key not in candidate_keys:
            raise DiagnosticError(
                E_REFERENCE_MISMATCH,
                "reference boundary has no matching deterministic candidate",
                field="boundary_truth.frame_index",
                details={"reference": row, "key": key},
            )
        if row.get("gold_label") not in valid_labels:
            raise DiagnosticError(
                E_REFERENCE_MISMATCH,
                "reference boundary label is not supported",
                field="boundary_truth.gold_label",
                details={"reference": row, "valid_labels": sorted(valid_labels)},
            )


def ensure_config(config: Config | None, **overrides: Any) -> Config:
    cfg = config or Config()
    if overrides:
        cfg = cfg.with_overrides(**overrides)
    cfg.validate()
    return cfg
