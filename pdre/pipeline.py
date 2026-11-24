"""File loading, deterministic batches, checkpoint continuation."""

from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import EngineConfig
from .incremental import IncrementalEngine
from .parsing import parse_jsonl_file
from .storage import CheckpointManager, Ledger, StorageError


@dataclass
class InputBundle:
    input_dir: Path
    clock_ms: int | None
    digest_algorithm: str
    events: list = field(default_factory=list)
    lineage: list = field(default_factory=list)
    policies: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    read_errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class BatchSpec:
    batch_id: str
    events: list
    lineage: list
    policies: list


def load_input_bundle(input_dir: str | Path) -> InputBundle:
    input_dir = Path(input_dir)
    config_path = input_dir / "sample_config.json"
    sample_config: dict[str, Any] = {}
    if config_path.exists():
        sample_config = json.loads(config_path.read_text(encoding="utf-8"))

    clock_ms = sample_config.get("observed_clock_ms")
    digest_algorithm = sample_config.get("digest_algorithm", "sha256")
    bundle = InputBundle(
        input_dir=input_dir,
        clock_ms=clock_ms,
        digest_algorithm=digest_algorithm,
    )

    file_specs = [
        ("events.jsonl", "event"),
        ("lineage.jsonl", "lineage"),
        ("policies_v1.jsonl", "policy_v1"),
        ("policies_v2.jsonl", "policy_v2"),
    ]
    for filename, category in file_specs:
        path = input_dir / filename
        if not path.exists():
            bundle.read_errors.append(
                {
                    "code": "E_INPUT_READ",
                    "source_category": category,
                    "source_name": filename,
                    "record_seq": 0,
                    "message": "input file does not exist",
                }
            )
            continue

        records, rejections, read_error = parse_jsonl_file(
            path,
            category,
            clock_ms=clock_ms,
            digest_algorithm=digest_algorithm,
        )
        bundle.rejections.extend(rejections)
        if read_error:
            bundle.read_errors.append(read_error)

        if category == "event":
            bundle.events.extend(records)
        elif category == "lineage":
            bundle.lineage.extend(records)
        else:
            bundle.policies.extend(records)

    return bundle


def build_batches(bundle: InputBundle, batch_size: int) -> list[BatchSpec]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    batches: list[BatchSpec] = []

    def add_chunks(prefix: str, records: list, kind: str) -> None:
        for chunk_index, start in enumerate(range(0, len(records), batch_size)):
            chunk = records[start : start + batch_size]
            batches.append(
                BatchSpec(
                    batch_id=f"{prefix}-{chunk_index:04d}",
                    events=chunk if kind == "events" else [],
                    lineage=chunk if kind == "lineage" else [],
                    policies=chunk if kind == "policies" else [],
                )
            )

    add_chunks("events", bundle.events, "events")
    add_chunks("lineage", bundle.lineage, "lineage")
    add_chunks("policies", bundle.policies, "policies")
    return batches


def _inject_residue_and_kill(manager: CheckpointManager) -> None:
    with manager.ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"ledger_seq": "half-written", "semantic": ')
        handle.flush()
        os.fsync(handle.fileno())
    residue_path = manager.snapshot_dir / ".fault-residue.json.tmp"
    residue_path.write_text('{"half_written": true,', encoding="utf-8")
    with residue_path.open("a", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    os.kill(os.getpid(), kill_signal)


def run_pipeline(
    bundle: InputBundle,
    *,
    checkpoint_dir: str | Path,
    snapshot_dir: str | Path,
    ledger_path: str | Path,
    config: EngineConfig | None = None,
    batch_size: int = 8,
    verify: bool = True,
    fault: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = config or EngineConfig()
    manager = CheckpointManager(
        checkpoint_dir,
        snapshot_dir=snapshot_dir,
        ledger_path=ledger_path,
        lock_timeout_ms=config.lock_timeout_ms,
    )
    recovery_info: dict[str, Any] | None = None
    storage_errors: list[dict[str, Any]] = []

    try:
        recovery = manager.recover()
    except StorageError as exc:
        if exc.code == "E_VERSION_MISMATCH":
            recovery_info = {
                "status": "full_rebuild_required",
                "reason": exc.to_dict(),
            }
            manager.reset_for_full_rebuild()
            recovery = None
        else:
            raise

    if recovery is not None:
        engine = IncrementalEngine.from_state(recovery.state, config=config)
        engine.rejections = list(bundle.rejections)
        engine.read_errors = list(bundle.read_errors)
        ledger_info = recovery.checkpoint
        ledger = Ledger(
            ledger_path,
            entry_count=ledger_info["entry_count"],
            tail_hash=ledger_info["tail_hash"],
        )
        recovery_info = {
            "status": "recovered",
            "checkpoint": {
                "checkpoint_id": ledger_info["checkpoint_id"],
                "watermark": ledger_info["watermark"],
                "batch_seq": ledger_info["batch_seq"],
            },
            "residue": recovery.residue,
            "replayed_ledger_entries": len(recovery.ledger_entries),
        }
    else:
        if manager.ledger_path.exists():
            manager.ledger_path.unlink()
        engine = IncrementalEngine(
            config=config,
            rejections=list(bundle.rejections),
            read_errors=list(bundle.read_errors),
        )
        ledger = Ledger(ledger_path)
        if recovery_info is None:
            recovery_info = {"status": "fresh_start"}

    batches = build_batches(bundle, batch_size)
    completed = set(engine.processed_batches)
    last_checkpoint_info: dict[str, Any] | None = None

    for batch_index, batch in enumerate(batches, start=1):
        if batch.batch_id in completed:
            continue

        report = engine.apply_batch(
            batch_id=batch.batch_id,
            events=batch.events,
            lineage=batch.lineage,
            policies=batch.policies,
            verify=verify,
        )
        recomputed_ids = report.get("incremental", {}).get("recomputed_unit_ids", [])
        recomputed_units = [
            engine.unit_cache[unit_id]
            for unit_id in recomputed_ids
            if unit_id in engine.unit_cache
        ]
        ledger_result = ledger.append_unit_judgments(
            recomputed_units,
            batch_id=batch.batch_id,
            unit_versions=engine.unit_versions,
        )
        state = engine.export_state()
        last_checkpoint_info = manager.save_checkpoint(
            watermark=batch.batch_id,
            batch_seq=batch_index,
            state=state,
            ledger=ledger_result,
        )
        completed.add(batch.batch_id)

        if fault == "residue-then-kill-after-first-checkpoint" and last_checkpoint_info:
            _inject_residue_and_kill(manager)

    if engine.last_report is None:
        report = engine.recompute(verify=verify)
    else:
        report = engine.last_report

    report["checkpoint_recovery"] = recovery_info
    report["last_checkpoint"] = last_checkpoint_info
    report["storage_errors"] = storage_errors
    return report, storage_errors
