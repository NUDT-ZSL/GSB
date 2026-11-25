from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import pretty_json, sha256_json
from .formats.v2 import write_v2_bundle
from .migrate import migrate_bundle, migrate_task
from .model import CandidateMask, ChunkPlan, UnifiedTask


MASTER_SEED = 20261126
BASE_TIME_UNIX_MS = 1760000000000


def _base_logits(rng: np.random.Generator, size: int, decimal_places: int = 6) -> list[float]:
    values = rng.normal(0.0, 1.25, size=size)
    return [round(float(value), decimal_places) for value in values]


def _task(
    *,
    case_id: str,
    offset_s: int,
    vocab_size: int,
    logits: list[float],
    k_mode: str,
    k_value: float,
    temperature: float,
    seed: int,
    excluded: list[int],
    num_samples: int = 4,
    top_p: float | None = None,
    error_budget: float = 1e-5,
    revision: int = 1,
    revision_id: str = "r-1",
) -> UnifiedTask:
    return UnifiedTask(
        case_id=case_id,
        effective_at_ms=BASE_TIME_UNIX_MS + offset_s * 1000,
        logits=tuple(logits),
        logits_shape=(vocab_size,),
        k_mode=k_mode,
        k_value=float(k_value),
        temperature=temperature,
        tie_policy="index_asc",
        seed=seed,
        candidate_mask=CandidateMask.flat(excluded, source="generated_flat"),
        error_budget=error_budget,
        revision=revision,
        revision_id=revision_id,
        chunk_plan=ChunkPlan.for_logits(vocab_size, 4),
        num_samples=num_samples,
        top_p=top_p,
        source_format="generated",
        source_ref=f"generator:{case_id}",
    )


def build_tasks(seed: int = MASTER_SEED) -> list[UnifiedTask]:
    rng = np.random.Generator(np.random.PCG64(seed))
    task_seed = int(rng.integers(0, 2**31 - 1))

    tie_logits = _base_logits(rng, 12)
    tie_logits[2] = tie_logits[1]
    tie_logits[5] = tie_logits[4]
    tie_logits[9] = 1.234567
    tie_logits[0] = 0.987654
    tie_case = _task(
        case_id="case_tie_topk_mask",
        offset_s=0,
        vocab_size=12,
        logits=tie_logits,
        k_mode="top_k",
        k_value=5,
        temperature=0.7,
        seed=task_seed,
        excluded=[2, 7],
    )

    top_p_logits = _base_logits(rng, 10)
    top_p_logits[0] = 2.4
    top_p_logits[1] = 1.9
    top_p_logits[4] = 2.0
    top_p_logits[7] = 1.4
    top_p_case = _task(
        case_id="case_topp_mask",
        offset_s=60,
        vocab_size=10,
        logits=top_p_logits,
        k_mode="top_p",
        k_value=0.72,
        temperature=0.9,
        seed=task_seed + 17,
        excluded=[0, 4],
    )

    combined_logits = _base_logits(rng, 13)
    combined_logits[2] = 2.2
    combined_logits[6] = 2.2
    combined_logits[10] = 1.6
    combined_case = _task(
        case_id="case_topk_topp_mask",
        offset_s=120,
        vocab_size=13,
        logits=combined_logits,
        k_mode="top_k",
        k_value=7,
        temperature=0.65,
        seed=task_seed + 41,
        excluded=[5, 8],
        top_p=0.8,
    )

    extreme_logits = [
        8.0,
        5.0,
        4.0,
        3.0,
        2.0,
        1.0,
        0.0,
        -2.0,
        -20.0,
        -58.0,
        -75.0,
    ]
    extreme_case = _task(
        case_id="case_precision_subnormal",
        offset_s=180,
        vocab_size=11,
        logits=extreme_logits,
        k_mode="top_k",
        k_value=10,
        temperature=0.7,
        seed=task_seed + 73,
        excluded=[3],
    )
    return [tie_case, top_p_case, combined_case, extreme_case]


def _v3_task(task: UnifiedTask) -> dict[str, Any]:
    excluded = list(task.candidate_mask.excluded_indices)
    if not excluded:
        selectors: list[dict[str, Any]] = []
    elif len(excluded) == 1:
        selectors = [
            {"selector_id": "outer", "op": "exclude", "groups": [
                {"selector_id": "leaf", "op": "exclude", "indices": excluded}
            ]}
        ]
    else:
        selectors = [
            {"selector_id": "direct", "op": "exclude", "indices": [excluded[0]]},
            {
                "selector_id": "nested",
                "op": "exclude",
                "groups": [
                    {"selector_id": "leaf", "op": "exclude", "indices": excluded[1:]}
                ],
            },
        ]
    return {
        "case": {
            "id": task.case_id,
            "effective": {"at_unix_ms": task.effective_at_ms},
        },
        "logits": {
            "dtype": "float64",
            "shape": list(task.logits_shape),
            "values": list(task.logits),
        },
        "sampling": {
            "mode": task.k_mode,
            "value": task.k_value,
            "temperature": task.temperature,
            "tie_policy": task.tie_policy,
            "seed": task.seed,
            "num_samples": task.num_samples,
            "top_p": task.top_p,
        },
        "candidate_mask": {
            "mask_version": 2,
            "selectors": selectors,
        },
        "error_budget": task.error_budget,
        "revision": {"number": task.revision, "revision_id": task.revision_id},
        "chunk_plan": task.chunk_plan.to_dict(),
    }


def _v1_jsonl(path: Path, tasks: list[UnifiedTask], base_time_ms: int) -> None:
    lines = [
        json.dumps(
            {
                "format": "task-v1-jsonl",
                "version": 1,
                "base_time_unix_ms": base_time_ms,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    ]
    for task in tasks:
        outcome = migrate_task(task, "v1", base_time_ms=base_time_ms)
        lines.append(json.dumps(outcome.payload, ensure_ascii=False, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_incremental(root: Path, task: UnifiedTask, revision: int) -> None:
    changed = dataclasses.replace(
        task,
        case_id=f"case_incremental_r{revision}",
        revision=revision,
        revision_id=f"r-{revision}",
        effective_at_ms=task.effective_at_ms + revision * 1000,
        source_ref=f"generator:incremental:{revision}",
    )
    document = {
        "format": "task-v3",
        "version": 3,
        "tasks": [_v3_task(changed)],
    }
    (root / "v3" / f"incremental_rev{revision}.json").write_text(pretty_json(document), encoding="utf-8")


def _write_invalid(root: Path, tasks: list[UnifiedTask]) -> None:
    invalid = root / "invalid"
    invalid.mkdir(parents=True, exist_ok=True)
    (invalid / "unknown_version.json").write_text(
        pretty_json({"format": "task-unknown", "version": 9, "tasks": []}),
        encoding="utf-8",
    )
    base_task = migrate_task(tasks[0], "v1", base_time_ms=BASE_TIME_UNIX_MS).payload
    duplicate_doc = {
        "format": "task-v1",
        "version": 1,
        "base_time_unix_ms": BASE_TIME_UNIX_MS,
        "tasks": [dict(base_task), dict(base_task)],
    }
    (invalid / "duplicate_case_v1.json").write_text(pretty_json(duplicate_doc), encoding="utf-8")
    missing_seed = dict(base_task)
    missing_seed.pop("seed")
    (invalid / "missing_seed_v1.json").write_text(
        pretty_json(
            {
                "format": "task-v1",
                "version": 1,
                "base_time_unix_ms": BASE_TIME_UNIX_MS,
                "tasks": [missing_seed],
            }
        ),
        encoding="utf-8",
    )
    shape_task = _v3_task(tasks[0])
    shape_task["case"] = {"id": "case_invalid_shape", "effective": {"at_unix_ms": BASE_TIME_UNIX_MS}}
    shape_task["logits"]["shape"] = [len(tasks[0].logits) + 1]
    (invalid / "shape_mismatch_v3.json").write_text(
        pretty_json({"format": "task-v3", "version": 3, "tasks": [shape_task]}),
        encoding="utf-8",
    )
    regression_tasks = [
        dataclasses.replace(
            tasks[0],
            case_id="case_regression_a",
            effective_at_ms=BASE_TIME_UNIX_MS + 60000,
            source_ref="generator:invalid:regression-a",
        ),
        dataclasses.replace(
            tasks[1],
            case_id="case_regression_b",
            effective_at_ms=BASE_TIME_UNIX_MS,
            source_ref="generator:invalid:regression-b",
        ),
    ]
    regression_dir = invalid / "v2_timestamp_regression"
    write_v2_bundle(regression_tasks, regression_dir, shard_size=2)
    mismatch_dir = invalid / "v2_metadata_mismatch"
    write_v2_bundle(tasks[:2], mismatch_dir, shard_size=2)
    conn = sqlite3.connect(mismatch_dir / "meta.db")
    try:
        conn.execute(
            "UPDATE tasks SET effective_at_ms = effective_at_ms + 1000 WHERE case_id = ?",
            (tasks[0].case_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _manifest(root: Path, tasks: list[UnifiedTask], seed: int) -> dict[str, Any]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = str(path.relative_to(root))
        if path.suffix == ".db":
            conn = sqlite3.connect(path)
            try:
                rows = []
                for table in ("dataset_meta", "shards", "tasks"):
                    try:
                        if table == "dataset_meta":
                            query = "SELECT key, value FROM dataset_meta ORDER BY key"
                        else:
                            query = f"SELECT * FROM {table} ORDER BY rowid"
                        rows.extend([list(row) for row in conn.execute(query)])
                    except sqlite3.Error:
                        rows.append([f"<missing:{table}>"])
                digest = hashlib.sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
            finally:
                conn.close()
            files[relative] = digest
        else:
            files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema": "sampling-sample-manifest-v1",
        "master_seed": seed,
        "base_time_unix_ms": BASE_TIME_UNIX_MS,
        "task_count": len(tasks),
        "case_ids": [task.case_id for task in tasks],
        "files": files,
        "logical_checksum": sha256_json(
            {
                "seed": seed,
                "base_time_unix_ms": BASE_TIME_UNIX_MS,
                "tasks": [task.semantic_projection() for task in tasks],
            }
        ),
    }


def generate_suite(root: str | Path, *, seed: int = MASTER_SEED) -> dict[str, Any]:
    root_path = Path(root)
    for name in ("v1", "v2", "v3", "invalid"):
        target = root_path / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks(seed)
    v1_bundle = migrate_bundle(tasks, "v1", policy=None)
    v1_document = {
        "format": "task-v1",
        "version": 1,
        "base_time_unix_ms": BASE_TIME_UNIX_MS,
        "tasks": v1_bundle.payload["tasks"],
    }
    (root_path / "v1" / "tasks.json").write_text(pretty_json(v1_document), encoding="utf-8")
    _v1_jsonl(root_path / "v1" / "tasks.jsonl", tasks, BASE_TIME_UNIX_MS)
    write_v2_bundle(tasks, root_path / "v2", shard_size=2)
    v3_document = {
        "format": "task-v3",
        "version": 3,
        "tasks": [_v3_task(task) for task in tasks],
    }
    (root_path / "v3" / "tasks.json").write_text(pretty_json(v3_document), encoding="utf-8")
    _write_incremental(root_path, tasks[0], 2)
    _write_incremental(root_path, tasks[0], 3)
    _write_invalid(root_path, tasks)
    manifest = _manifest(root_path, tasks, seed)
    (root_path / "manifest.json").write_text(pretty_json(manifest), encoding="utf-8")
    return manifest

