"""Single-process serial truth replay and reconciliation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blocks import BlockStore
from .engine import RuntimeEngine
from .errors import BusinessError, ErrorCode
from .models import Scenario
from .paths import assert_safe_delete
from .storage import StateStore


@dataclass
class VerifyResult:
    consistent: bool
    primary_run_id: str
    truth_run_id: str
    compared_batches: int
    mismatches: list[dict[str, Any]]
    drift_evidence: dict[str, Any]
    primary: dict[str, Any]
    truth: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.consistent,
            "error_code": None if self.consistent else ErrorCode.VERIFY_MISMATCH,
            "command": "verify",
            "consistent": self.consistent,
            "primary_run_id": self.primary_run_id,
            "truth_run_id": self.truth_run_id,
            "compared_batches": self.compared_batches,
            "mismatches": self.mismatches,
            "drift_evidence": self.drift_evidence,
            "primary": self.primary,
            "truth": self.truth,
        }


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def verify_against_serial(
    scenario: Scenario,
    *,
    db_path: str | Path,
    blocks_path: str | Path,
    truth_db_path: str | Path,
    truth_blocks_path: str | Path,
    reset_truth: bool = True,
) -> VerifyResult:
    primary_db = Path(db_path)
    if not primary_db.exists():
        raise BusinessError(
            ErrorCode.NOT_FOUND,
            "primary database does not exist",
            details={"db_path": str(primary_db)},
        )
    primary_store = StateStore(primary_db, primary_db.with_suffix(primary_db.suffix + ".lock"))
    primary_run = primary_store.require_run(scenario.plan.plan_id)
    primary_batches = primary_store.list_batches(primary_run)
    expected_truth_batches = len(primary_batches)
    truth_db = Path(truth_db_path)
    truth_blocks = Path(truth_blocks_path)
    if reset_truth:
        assert_safe_delete(
            truth_db,
            protected=[primary_db, Path(blocks_path), Path(truth_blocks_path)],
        )
        assert_safe_delete(
            truth_blocks,
            protected=[primary_db, Path(blocks_path), truth_db],
        )
        for suffix in ("", "-wal", "-shm"):
            _remove_path(Path(str(truth_db) + suffix))
        _remove_path(truth_blocks)
        _remove_path(truth_db.with_suffix(truth_db.suffix + ".lock"))
    truth_engine = RuntimeEngine(
        scenario,
        db_path=truth_db,
        blocks_path=truth_blocks,
    )
    try:
        truth_result = truth_engine.run(
            resume=False,
            max_batches=expected_truth_batches,
        )
    finally:
        truth_engine.close()
    truth_store = StateStore(truth_db, truth_db.with_suffix(truth_db.suffix + ".lock"))
    truth_run = truth_store.require_run(scenario.plan.plan_id)
    mismatches: list[dict[str, Any]] = []
    mismatches.extend(primary_store.compare_batches_with(primary_run, truth_store, truth_run))
    primary_shapes = primary_store.list_shapes(primary_run)
    truth_shapes = truth_store.list_shapes(truth_run)
    if len(primary_shapes) != len(truth_shapes):
        mismatches.append(
            {
                "field": "shape_version_count",
                "left": len(primary_shapes),
                "right": len(truth_shapes),
            }
        )
    else:
        for left, right in zip(primary_shapes, truth_shapes):
            for field in ("version", "parent_version", "content_digest", "action"):
                if left[field] != right[field]:
                    mismatches.append(
                        {
                            "field": f"shape.{field}",
                            "version": left["version"],
                            "left": left[field],
                            "right": right[field],
                        }
                    )
    primary_status = primary_store.status(primary_run)
    truth_status = truth_store.status(truth_run)
    if primary_status["watermark_seq"] != truth_status["watermark_seq"]:
        mismatches.append(
            {
                "field": "watermark_seq",
                "left": primary_status["watermark_seq"],
                "right": truth_status["watermark_seq"],
            }
        )
    if primary_status["current_shape"]["content_digest"] != truth_status["current_shape"]["content_digest"]:
        mismatches.append(
            {
                "field": "current_shape_digest",
                "left": primary_status["current_shape"]["content_digest"],
                "right": truth_status["current_shape"]["content_digest"],
            }
        )
    primary_decisions = primary_store.list_decisions(primary_run)
    truth_decisions = truth_store.list_decisions(truth_run)
    if len(primary_decisions) != len(truth_decisions):
        mismatches.append(
            {
                "field": "decision_count",
                "left": len(primary_decisions),
                "right": len(truth_decisions),
            }
        )
    else:
        for left, right in zip(primary_decisions, truth_decisions):
            if left["status"] != right["status"] or left["error_code"] != right["error_code"]:
                mismatches.append(
                    {
                        "field": "decision",
                        "batch_id": left["batch_id"],
                        "left": [left["status"], left["error_code"]],
                        "right": [right["status"], right["error_code"]],
                    }
                )
            left_selected = left["payload"].get("selected", {}).get("content_digest")
            right_selected = right["payload"].get("selected", {}).get("content_digest")
            if left_selected != right_selected:
                mismatches.append(
                    {
                        "field": "decision.selected_shape",
                        "batch_id": left["batch_id"],
                        "left": left_selected,
                        "right": right_selected,
                    }
                )
    block_store = BlockStore(blocks_path)
    for batch in primary_batches:
        if batch.get("block_path") and not block_store.verify_manifest(
            batch["block_path"], batch["output_digest"]
        ):
            mismatches.append(
                {
                    "batch_id": batch["batch_id"],
                    "field": "block_manifest",
                    "left": batch.get("block_path"),
                    "right": "missing_or_incomplete",
                }
            )
    triggers = primary_store.list_events(primary_run, "drift_trigger")
    suppressed = primary_store.list_events(primary_run, "suppressed_reoptimization")
    truth_triggers = truth_store.list_events(truth_run, "drift_trigger")
    truth_suppressed = truth_store.list_events(truth_run, "suppressed_reoptimization")
    if len(triggers) != len(truth_triggers):
        mismatches.append(
            {"field": "drift_trigger_count", "left": len(triggers), "right": len(truth_triggers)}
        )
    if len(suppressed) != len(truth_suppressed):
        mismatches.append(
            {
                "field": "suppressed_count",
                "left": len(suppressed),
                "right": len(truth_suppressed),
            }
        )
    trigger_signature = [
        (
            event["batch_id"],
            event["payload"].get("primary", {}).get("source_table"),
            event["payload"].get("primary", {}).get("source_key"),
            event["payload"].get("primary", {}).get("batch_interval"),
            event["payload"].get("max_deviation_ratio"),
            event["payload"].get("threshold_margin"),
        )
        for event in triggers
    ]
    truth_trigger_signature = [
        (
            event["batch_id"],
            event["payload"].get("primary", {}).get("source_table"),
            event["payload"].get("primary", {}).get("source_key"),
            event["payload"].get("primary", {}).get("batch_interval"),
            event["payload"].get("max_deviation_ratio"),
            event["payload"].get("threshold_margin"),
        )
        for event in truth_triggers
    ]
    if trigger_signature != truth_trigger_signature:
        mismatches.append(
            {
                "field": "drift_trigger_evidence",
                "left": trigger_signature,
                "right": truth_trigger_signature,
            }
        )
    suppressed_signature = [
        (
            event["batch_id"],
            event["payload"].get("reason"),
            event["payload"].get("remaining_cooldown_ms"),
        )
        for event in suppressed
    ]
    truth_suppressed_signature = [
        (
            event["batch_id"],
            event["payload"].get("reason"),
            event["payload"].get("remaining_cooldown_ms"),
        )
        for event in truth_suppressed
    ]
    if suppressed_signature != truth_suppressed_signature:
        mismatches.append(
            {
                "field": "suppressed_request_evidence",
                "left": suppressed_signature,
                "right": truth_suppressed_signature,
            }
        )
    drift_evidence = {
        "trigger_count": len(triggers),
        "suppressed_count": len(suppressed),
        "triggers": [
            {
                "batch_id": event["batch_id"],
                "arrival_seq": event["arrival_seq"],
                "threshold": event["payload"].get("threshold"),
                "max_deviation_ratio": event["payload"].get("max_deviation_ratio"),
                "threshold_margin": event["payload"].get("threshold_margin"),
                "source_table": event["payload"].get("primary", {}).get("source_table"),
                "source_key": event["payload"].get("primary", {}).get("source_key"),
                "batch_interval": event["payload"].get("primary", {}).get("batch_interval"),
                "error_ledger": event["payload"].get("primary", {}).get("error_ledger"),
            }
            for event in triggers
        ],
        "suppressed": [
            {
                "batch_id": event["batch_id"],
                "reason": event["payload"].get("reason"),
                "remaining_cooldown_ms": event["payload"].get("remaining_cooldown_ms"),
                "max_deviation_ratio": event["payload"].get("max_deviation_ratio"),
            }
            for event in suppressed
        ],
        "truth_trigger_count": len(truth_triggers),
        "truth_suppressed_count": len(truth_suppressed),
    }
    result = VerifyResult(
        consistent=not mismatches,
        primary_run_id=primary_run,
        truth_run_id=truth_run,
        compared_batches=len(primary_batches),
        mismatches=mismatches,
        drift_evidence=drift_evidence,
        primary={
            "watermark_seq": primary_status["watermark_seq"],
            "current_shape": primary_status["current_shape"],
            "committed_batches": len(primary_batches),
        },
        truth={
            "watermark_seq": truth_status["watermark_seq"],
            "current_shape": truth_status["current_shape"],
            "committed_batches": len(truth_store.list_batches(truth_run)),
            "processed_batches": len(truth_result.processed_batches),
        },
    )
    primary_store.close()
    truth_store.close()
    return result

