"""Manifest parsing, strict input validation, and structural block planning."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import input_error
from .hashing import array_sha256, sha256_json
from .models import (
    ArraySet,
    BlockPlan,
    BlockRange,
    TaskSpec,
    make_case_cache_scope,
)


@dataclass(frozen=True)
class PreparedTask:
    task: TaskSpec
    arrays: ArraySet
    validation_meta: dict[str, Any]
    structural_plan: dict[str, Any]
    plans: tuple[BlockPlan, ...]
    q_block_hashes: dict[int, tuple[str, ...]]
    kv_block_hashes: dict[int, tuple[str, ...]]
    head_hashes: dict[str, tuple[str, ...]]

    @property
    def structural_sha256(self) -> str:
        return self.structural_plan["structural_sha256"]


def load_manifest(manifest_path: str | Path) -> list[TaskSpec]:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise input_error("manifest file does not exist", path=str(path))

    tasks: list[TaskSpec] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise input_error(
                    "manifest line is not valid JSON",
                    path=str(path),
                    line=line_number,
                    error=str(exc),
                ) from exc
            if not isinstance(record, dict):
                raise input_error(
                    "manifest line must be a JSON object",
                    path=str(path),
                    line=line_number,
                )
            task = TaskSpec.from_record(record, path.parent)
            if task.case_id in seen:
                raise input_error(
                    "duplicate case_id in manifest",
                    case_id=task.case_id,
                    line=line_number,
                )
            seen.add(task.case_id)
            tasks.append(task)
    if not tasks:
        raise input_error("manifest contains no tasks", path=str(path))
    return tasks


def _load_array(path: Path, label: str) -> np.ndarray:
    if not path.is_file():
        raise input_error(f"{label} array file does not exist", path=str(path))
    try:
        array = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise input_error(
            f"{label} array could not be loaded",
            path=str(path),
            error=str(exc),
        ) from exc
    if not isinstance(array, np.ndarray):
        raise input_error(f"{label} file is not a NumPy array", path=str(path))
    return array


def array_set_from_qkv(
    task: TaskSpec,
    q: np.ndarray,
    k: np.ndarray,
    v: np.ndarray,
) -> ArraySet:
    expected_shape = (task.num_heads, task.seq_len, task.head_dim)

    for label, array in (("Q", q), ("K", k), ("V", v)):
        if array.shape != expected_shape:
            raise input_error(
                f"{label} shape does not match task dimensions",
                array=label,
                actual_shape=list(array.shape),
                expected_shape=list(expected_shape),
                case_id=task.case_id,
            )
        if array.dtype.name != task.dtype:
            raise input_error(
                f"{label} dtype does not match task dtype",
                array=label,
                actual_dtype=str(array.dtype),
                expected_dtype=task.dtype,
                case_id=task.case_id,
            )
        if not np.all(np.isfinite(array)):
            raise input_error(
                f"{label} contains non-finite values",
                array=label,
                case_id=task.case_id,
            )

    q_sha = array_sha256(q)
    k_sha = array_sha256(k)
    v_sha = array_sha256(v)
    qkv_sha = sha256_json(
        {
            "q": q_sha,
            "k": k_sha,
            "v": v_sha,
            "shape": list(expected_shape),
            "dtype": task.dtype,
        }
    )
    return ArraySet(
        q=q,
        k=k,
        v=v,
        q_sha256=q_sha,
        k_sha256=k_sha,
        v_sha256=v_sha,
        qkv_sha256=qkv_sha,
    )


def load_arrays(task: TaskSpec) -> ArraySet:
    q = _load_array(task.q_path, "Q")
    k = _load_array(task.k_path, "K")
    v = _load_array(task.v_path, "V")
    return array_set_from_qkv(task, q, k, v)


def _block_ranges(length: int, block_len: int) -> tuple[BlockRange, ...]:
    return tuple(
        BlockRange(
            index=index,
            start=start,
            length=min(block_len, length - start),
        )
        for index, start in enumerate(range(0, length, block_len))
    )


def _range_dicts(blocks: tuple[BlockRange, ...]) -> list[dict[str, int]]:
    return [
        {"index": block.index, "start": block.start, "length": block.length}
        for block in blocks
    ]


def _head_hashes(array: np.ndarray) -> tuple[str, ...]:
    return tuple(array_sha256(array[head]) for head in range(array.shape[0]))


def _block_hashes(
    array: np.ndarray,
    ranges: tuple[BlockRange, ...],
) -> dict[int, tuple[str, ...]]:
    return {
        head: tuple(array_sha256(array[head, block.start : block.stop]) for block in ranges)
        for head in range(array.shape[0])
    }


def prepare_arrays(task: TaskSpec, arrays: ArraySet) -> PreparedTask:
    q_blocks = _block_ranges(task.seq_len, task.q_block_len)
    kv_blocks = _block_ranges(task.seq_len, task.kv_block_len)

    plans: list[BlockPlan] = []
    for head in range(task.num_heads):
        for q_block in q_blocks:
            visible = tuple(
                kv_block
                for kv_block in kv_blocks
                if not task.causal or kv_block.start <= q_block.stop - 1
            )
            if not visible:
                raise input_error(
                    "causal planning produced a Q block without visible KV blocks",
                    case_id=task.case_id,
                    head=head,
                    q_block=q_block.index,
                )
            plans.append(
                BlockPlan(
                    head=head,
                    q_block=q_block.index,
                    q_start=q_block.start,
                    q_len=q_block.length,
                    kv_blocks=visible,
                )
            )

    q_block_hashes = _block_hashes(arrays.q, q_blocks)
    k_block_hashes = _block_hashes(arrays.k, kv_blocks)
    v_block_hashes = _block_hashes(arrays.v, kv_blocks)
    head_hashes = {
        "q": _head_hashes(arrays.q),
        "k": _head_hashes(arrays.k),
        "v": _head_hashes(arrays.v),
    }

    validation_meta: dict[str, Any] = {
        "schema_version": 1,
        "task": task.canonical(),
        "scope": make_case_cache_scope(task),
        "shape": [task.num_heads, task.seq_len, task.head_dim],
        "array_sha256": {
            "q": arrays.q_sha256,
            "k": arrays.k_sha256,
            "v": arrays.v_sha256,
            "qkv": arrays.qkv_sha256,
        },
        "head_sha256": {
            "q": list(head_hashes["q"]),
            "k": list(head_hashes["k"]),
            "v": list(head_hashes["v"]),
        },
        "q_block_sha256": {
            str(head): list(hashes) for head, hashes in q_block_hashes.items()
        },
        "kv_block_sha256": {
            str(head): {
                "k": list(k_block_hashes[head]),
                "v": list(v_block_hashes[head]),
            }
            for head in range(task.num_heads)
        },
        "plan_meta": {
            "q_blocks": _range_dicts(q_blocks),
            "kv_blocks": _range_dicts(kv_blocks),
            "causal": task.causal,
        },
    }

    structural_payload: dict[str, Any] = {
        "schema_version": 1,
        "case_id": task.case_id,
        "num_heads": task.num_heads,
        "seq_len": task.seq_len,
        "head_dim": task.head_dim,
        "q_block_len": task.q_block_len,
        "kv_block_len": task.kv_block_len,
        "causal": task.causal,
        "q_blocks": _range_dicts(q_blocks),
        "kv_blocks": _range_dicts(kv_blocks),
        "plans": [
            {
                "head": plan.head,
                "q_block": plan.q_block,
                "q_start": plan.q_start,
                "q_len": plan.q_len,
                "kv_blocks": [
                    {
                        "index": block.index,
                        "start": block.start,
                        "length": block.length,
                    }
                    for block in plan.kv_blocks
                ],
            }
            for plan in plans
        ],
    }
    structural_payload["structural_sha256"] = sha256_json(structural_payload)

    return PreparedTask(
        task=task,
        arrays=arrays,
        validation_meta=validation_meta,
        structural_plan=structural_payload,
        plans=tuple(plans),
        q_block_hashes=q_block_hashes,
        kv_block_hashes={
            head: tuple(
                sha256_json(
                    {
                        "k": k_block_hashes[head][index],
                        "v": v_block_hashes[head][index],
                    }
                )
                for index in range(len(kv_blocks))
            )
            for head in range(task.num_heads)
        },
        head_hashes=head_hashes,
    )


def prepare_task(task: TaskSpec) -> PreparedTask:
    return prepare_arrays(task, load_arrays(task))


def prepare_manifest(manifest_path: str | Path) -> list[PreparedTask]:
    """Load and validate every task before the engine commits any snapshot."""

    return [prepare_task(task) for task in load_manifest(manifest_path)]


def changed_heads(previous_validation: dict[str, Any], current: PreparedTask) -> list[int]:
    previous = previous_validation.get("head_sha256", {})
    changed: list[int] = []
    for head in range(current.task.num_heads):
        previous_q = previous.get("q", [])
        previous_k = previous.get("k", [])
        previous_v = previous.get("v", [])
        if (
            head >= len(previous_q)
            or head >= len(previous_k)
            or head >= len(previous_v)
            or previous_q[head] != current.head_hashes["q"][head]
            or previous_k[head] != current.head_hashes["k"][head]
            or previous_v[head] != current.head_hashes["v"][head]
        ):
            changed.append(head)
    return changed


def affected_q_blocks_from_hashes(
    previous_validation: dict[str, Any],
    current: PreparedTask,
) -> list[str]:
    """Find Q blocks whose Q content or K/V-visible score inputs changed."""

    affected: set[tuple[int, int]] = set()
    previous_q_blocks = previous_validation.get("q_block_sha256", {})
    previous_kv_blocks = previous_validation.get("kv_block_sha256", {})

    for plan in current.plans:
        head_key = str(plan.head)
        current_q = current.q_block_hashes[plan.head][plan.q_block]
        prior_q_list = previous_q_blocks.get(head_key, [])
        if plan.q_block >= len(prior_q_list) or prior_q_list[plan.q_block] != current_q:
            affected.add((plan.head, plan.q_block))
            continue

        prior_kv = previous_kv_blocks.get(head_key, {})
        prior_k = prior_kv.get("k", [])
        prior_v = prior_kv.get("v", [])
        for kv_block in plan.kv_blocks:
            index = kv_block.index
            current_k = current.kv_block_hashes.get(plan.head, ())
            # The per-KV combined hash is enough to detect K or V change.
            current_combined = (
                current.kv_block_hashes[plan.head][index]
                if index < len(current.kv_block_hashes.get(plan.head, ()))
                else None
            )
            prior_combined = None
            if index < len(prior_k) and index < len(prior_v):
                prior_combined = sha256_json({"k": prior_k[index], "v": prior_v[index]})
            if prior_combined is None or prior_combined != current_combined:
                affected.add((plan.head, plan.q_block))
                break

    return [f"h{head}:q{q_block}" for head, q_block in sorted(affected)]

