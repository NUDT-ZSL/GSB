from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .checkpoint_io import checkpoint_from_v2, read_v1_checkpoint
from .errors import (
    bad_checkpoint,
    chain_break,
    checkpoint_missing,
    policy_error,
    replay_generation_error,
)
from .models import Checkpoint, CheckpointSet, Policy, Sample, WalRecord
from .wal import parse_wal


def _discover_checkpoints(sample_root: Path) -> dict[int, Checkpoint]:
    checkpoint_root = sample_root / "checkpoints"
    v1_files = list((checkpoint_root / "v1").glob("*.json")) if (checkpoint_root / "v1").is_dir() else []
    if not v1_files and checkpoint_root.is_dir():
        v1_files = [
            path
            for path in checkpoint_root.glob("*.json")
            if path.name != "policy.json"
        ]
    v2_manifests = (
        list((checkpoint_root / "v2").glob("*/manifest.json"))
        if (checkpoint_root / "v2").is_dir()
        else []
    )
    if not v1_files and not v2_manifests:
        raise checkpoint_missing(
            "no v1 or v2 checkpoint was found",
            sample=str(sample_root.name),
        )

    checkpoints: dict[int, Checkpoint] = {}
    for path in sorted(v1_files):
        checkpoint = read_v1_checkpoint(path)
        if checkpoint.checkpoint_gen in checkpoints:
            raise bad_checkpoint(
                "duplicate checkpoint generation",
                checkpoint_gen=checkpoint.checkpoint_gen,
            )
        checkpoints[checkpoint.checkpoint_gen] = checkpoint
    for manifest in sorted(v2_manifests):
        checkpoint, _ = checkpoint_from_v2(manifest)
        if checkpoint.checkpoint_gen in checkpoints:
            raise bad_checkpoint(
                "duplicate checkpoint generation",
                checkpoint_gen=checkpoint.checkpoint_gen,
            )
        checkpoints[checkpoint.checkpoint_gen] = checkpoint
    if not checkpoints:
        raise checkpoint_missing("no usable checkpoint was found")
    return checkpoints


def _validate_checkpoint_chain(
    checkpoints: Mapping[int, Checkpoint],
    wal: list[WalRecord],
) -> None:
    tx_by_id = {record.logical_tx_id: record for record in wal}
    max_tx = max(tx_by_id, default=0)
    previous_v2: Checkpoint | None = None
    for generation in sorted(checkpoints):
        checkpoint = checkpoints[generation]
        if checkpoint.applied_watermark > max_tx:
            raise chain_break(
                "checkpoint watermark exceeds the available WAL",
                checkpoint_gen=generation,
                applied_watermark=checkpoint.applied_watermark,
                max_tx=max_tx,
            )
        if checkpoint.applied_watermark == 0:
            if checkpoint.chain_hash != "0" * 64:
                raise chain_break(
                    "zero-watermark checkpoint must carry the zero chain hash",
                    checkpoint_gen=generation,
                )
        else:
            record = tx_by_id.get(checkpoint.applied_watermark)
            if record is None:
                first = wal[0]
                if not (
                    first.prev_tx_id == checkpoint.applied_watermark
                    and first.prev_tx_hash == checkpoint.chain_hash
                ):
                    raise chain_break(
                        "checkpoint watermark transaction is absent and no exact WAL anchor exists",
                        checkpoint_gen=generation,
                        applied_watermark=checkpoint.applied_watermark,
                    )
            elif record.tx_hash != checkpoint.chain_hash:
                raise chain_break(
                    "checkpoint chain hash does not match its watermark transaction",
                    checkpoint_gen=generation,
                    txid=checkpoint.applied_watermark,
                )
        if checkpoint.format_version == "v2":
            if (
                checkpoint.parent_checkpoint_gen is not None
                and checkpoint.parent_checkpoint_gen not in checkpoints
            ):
                raise checkpoint_missing(
                    "v2 parent checkpoint generation is absent",
                    checkpoint_gen=checkpoint.checkpoint_gen,
                    parent_checkpoint_gen=checkpoint.parent_checkpoint_gen,
                )
            if previous_v2 is not None:
                if checkpoint.replay_generation < previous_v2.replay_generation:
                    raise replay_generation_error(
                        "v2 replay generation is not monotonic",
                        previous=previous_v2.replay_generation,
                        current=checkpoint.replay_generation,
                        checkpoint_gen=checkpoint.checkpoint_gen,
                    )
                if checkpoint.commit_ts_ms < previous_v2.commit_ts_ms:
                    raise bad_checkpoint(
                        "v2 absolute commit timestamps are not monotonic",
                        previous=previous_v2.commit_ts_ms,
                        current=checkpoint.commit_ts_ms,
                        checkpoint_gen=checkpoint.checkpoint_gen,
                    )
                if checkpoint.manifest_version < previous_v2.manifest_version:
                    raise bad_checkpoint(
                        "v2 manifest version is not monotonic",
                        previous=previous_v2.manifest_version,
                        current=checkpoint.manifest_version,
                    )
            previous_v2 = checkpoint

    first = wal[0]
    if first.prev_tx_id != 0:
        anchors = [
            checkpoint
            for checkpoint in checkpoints.values()
            if checkpoint.applied_watermark == first.prev_tx_id
            and checkpoint.chain_hash == first.prev_tx_hash
        ]
        if not anchors:
            raise chain_break(
                "first WAL transaction does not chain from a checkpoint anchor",
                txid=first.logical_tx_id,
                prev_tx_id=first.prev_tx_id,
            )
    for record in wal:
        if record.checkpoint_gen not in checkpoints:
            raise checkpoint_missing(
                "WAL dependency checkpoint generation is absent",
                txid=record.logical_tx_id,
                checkpoint_gen=record.checkpoint_gen,
            )


def _load_policy(sample_root: Path, overrides: Mapping[str, Any] | None) -> Policy:
    path = sample_root / "policy.json"
    if not path.is_file():
        raise policy_error("policy.json is missing", sample=str(sample_root.name))
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise policy_error("cannot parse policy.json", reason=str(exc)) from exc
    if not isinstance(raw, dict):
        raise policy_error("policy.json must contain an object")

    allowed = {
        "max_concurrent_txs",
        "round_budget_ms",
        "checkpoint_interval_ms",
        "clock_step_ms",
        "clock_tolerance_ms",
        "inject_failure_tx_ids",
        "fail_round_on_error",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise policy_error("policy.json contains unknown fields", fields=unknown)
    values = {
        "max_concurrent_txs": raw.get("max_concurrent_txs", 3),
        "round_budget_ms": raw.get("round_budget_ms", 24),
        "checkpoint_interval_ms": raw.get("checkpoint_interval_ms", 30),
        "clock_step_ms": raw.get("clock_step_ms", 8),
        "clock_tolerance_ms": raw.get("clock_tolerance_ms", 4),
        "inject_failure_tx_ids": tuple(raw.get("inject_failure_tx_ids", ())),
        "fail_round_on_error": raw.get("fail_round_on_error", True),
    }
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})
    for field in (
        "max_concurrent_txs",
        "round_budget_ms",
        "checkpoint_interval_ms",
        "clock_step_ms",
        "clock_tolerance_ms",
    ):
        value = values[field]
        minimum = 1 if field in {"max_concurrent_txs", "round_budget_ms"} else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise policy_error(
                f"{field} must be an integer >= {minimum}",
                field=field,
            )
    failures = values["inject_failure_tx_ids"]
    if not isinstance(failures, tuple) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in failures
    ):
        raise policy_error("inject_failure_tx_ids must contain positive integers")
    if not isinstance(values["fail_round_on_error"], bool):
        raise policy_error("fail_round_on_error must be a boolean")
    return Policy(**values)


def parse_sample(
    sample_dir: str | Path,
    *,
    policy_overrides: Mapping[str, Any] | None = None,
) -> Sample:
    root = Path(sample_dir).resolve()
    if not root.is_dir():
        raise bad_checkpoint("sample directory does not exist", sample=str(sample_dir))
    checkpoints_by_gen = _discover_checkpoints(root)
    wal = parse_wal(root)
    _validate_checkpoint_chain(checkpoints_by_gen, wal)
    policy = _load_policy(root, policy_overrides)
    current = max(checkpoints_by_gen.values(), key=lambda item: item.checkpoint_gen)
    return Sample(
        root=root,
        wal=wal,
        checkpoints=CheckpointSet(by_gen=checkpoints_by_gen, current=current),
        policy=policy,
    )
