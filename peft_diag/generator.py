"""Fixed-seed deterministic local sample generator."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    normalize_number,
    sha256_bytes,
    stable_uint,
    tree_digest,
)
from .snapshots import (
    LayerState,
    Snapshot,
    append_v2_incremental_revision,
    write_v1,
    write_v2,
)
from .tasks import (
    MODEL_REGISTRY,
    NormalizedTask,
    adapter_structure,
    normalize_task,
    parse_tasks,
)


DEFAULT_SEED = 20261127
TIME_ORIGIN_MS = 1_700_000_000_000

CASE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "case_id": "case-001",
        "base_model_rev": "r1",
        "adapter_type": "lora",
        "adapter_dim": 4,
        "lr": 0.002,
        "steps": 4,
        "micro_batch": 2,
        "logical_clock_ms": 1500,
        "revision": 0,
    },
    {
        "case_id": "case-002",
        "base_model_rev": "r1",
        "adapter_type": "qlora",
        "adapter_dim": 6,
        "lr": 0.0015,
        "steps": 6,
        "micro_batch": 3,
        "logical_clock_ms": 2500,
        "revision": 0,
    },
    {
        "case_id": "case-003",
        "base_model_rev": "r2",
        "adapter_type": "lora",
        "adapter_dim": 8,
        "lr": 0.001,
        "steps": 6,
        "micro_batch": 2,
        "logical_clock_ms": 3600,
        "revision": 0,
    },
    {
        "case_id": "case-004",
        "base_model_rev": "r2",
        "adapter_type": "qlora",
        "adapter_dim": 8,
        "lr": 0.003,
        "steps": 8,
        "micro_batch": 4,
        "logical_clock_ms": 4700,
        "revision": 0,
    },
    {
        "case_id": "case-005",
        "base_model_rev": "r1",
        "adapter_type": "lora",
        "adapter_dim": 2,
        "lr": 0.0005,
        "steps": 2,
        "micro_batch": 2,
        "logical_clock_ms": 5900,
        "revision": 0,
    },
    {
        "case_id": "case-006",
        "base_model_rev": "r2",
        "adapter_type": "lora",
        "adapter_dim": 12,
        "lr": 0.0008,
        "steps": 8,
        "micro_batch": 4,
        "logical_clock_ms": 6100,
        "revision": 0,
    },
)


def _task_dict(blueprint: dict[str, Any], seed: int) -> dict[str, Any]:
    model = MODEL_REGISTRY[blueprint["base_model_rev"]]
    _, _, _, trainable = adapter_structure(
        base_model_rev=blueprint["base_model_rev"],
        adapter_type=blueprint["adapter_type"],
        adapter_dim=blueprint["adapter_dim"],
        num_layers=model["num_layers"],
    )
    return {
        "task_version": "v1",
        "case_id": blueprint["case_id"],
        "base_model_rev": blueprint["base_model_rev"],
        "adapter_type": blueprint["adapter_type"],
        "adapter_dim": blueprint["adapter_dim"],
        "num_layers": model["num_layers"],
        "lr": blueprint["lr"],
        "steps": blueprint["steps"],
        "micro_batch": blueprint["micro_batch"],
        "seed": stable_uint("task-seed", seed, blueprint["case_id"], bits=32),
        "revision": blueprint["revision"],
        "logical_clock_ms": blueprint["logical_clock_ms"],
        "expected_trainable_params": trainable,
    }


def _array_for_layer(
    task: NormalizedTask,
    layer_key: str,
    *,
    revision: int,
    generation: int,
    channel: int,
) -> np.ndarray:
    spec = next(spec for spec in task.layer_specs if spec.key == layer_key)
    rows, columns = spec.shape
    count = rows * columns
    index = np.arange(count, dtype=np.float64)
    phase = stable_uint("snapshot", task.case_id, layer_key, task.seed, revision, generation, channel, bits=64)
    phase_a = (phase % 100_003) / 100_003.0
    phase_b = ((phase >> 17) % 99_991) / 99_991.0
    wave = np.sin((index + 1.0) * (0.011 + 0.003 * phase_a) + phase_b * 4.0)
    cosine = np.cos((index + 1.0) * (0.007 + 0.002 * phase_b) + phase_a * 3.0)
    base_scale = 0.012 if channel == 0 else (0.0025 if channel == 1 else 0.000009)
    values = base_scale * (wave + 0.35 * cosine)
    if channel == 2:
        values = np.abs(values) + 1e-8
    return np.asarray(
        [normalize_number(value) for value in values.tolist()],
        dtype=np.float64,
    ).reshape(rows, columns)


def generate_snapshot(
    task: NormalizedTask,
    *,
    revision: int,
    generation: int = 0,
    absolute_ms: int | None = None,
) -> Snapshot:
    if revision < 0:
        raise ValueError("revision must be non-negative")
    layers: dict[str, LayerState] = {}
    for spec in task.layer_specs:
        layers[spec.key] = LayerState(
            key=spec.key,
            layer_idx=spec.layer_idx,
            shape=spec.shape,
            values=_array_for_layer(
                task,
                spec.key,
                revision=revision,
                generation=generation,
                channel=0,
            ),
            momentum=_array_for_layer(
                task,
                spec.key,
                revision=revision,
                generation=generation,
                channel=1,
            ),
            variance=_array_for_layer(
                task,
                spec.key,
                revision=revision,
                generation=generation,
                channel=2,
            ),
        )
    snapshot = Snapshot(
        snapshot_id=f"snap-{task.case_id}-r{revision}-g{generation}",
        format_version="v2",
        case_id=task.case_id,
        base_model_rev=task.base_model_rev,
        adapter_type=task.adapter_type,
        adapter_dim=task.adapter_dim,
        num_layers=task.num_layers,
        seed=task.seed,
        revision=revision,
        time_origin_ms=TIME_ORIGIN_MS,
        absolute_ms=TIME_ORIGIN_MS + (absolute_ms if absolute_ms is not None else task.logical_clock_ms),
        generation=generation,
        layers=layers,
        tombstones=[],
        provenance={"format": "native", "generator_seed": DEFAULT_SEED},
        source_format="native",
    )
    snapshot.validate_against_task(task)
    return snapshot


def _write_invalid_samples(root: Path, base_tasks: list[NormalizedTask]) -> dict[str, str]:
    invalid = root / "invalid"
    invalid.mkdir(parents=True, exist_ok=True)
    first = base_tasks[0].canonical_fields()
    records: dict[str, str] = {}

    duplicate = dict(first)
    duplicate["logical_clock_ms"] = duplicate["logical_clock_ms"] + 1
    payload = "\n".join(
        canonical_bytes(item).decode("utf-8") for item in (first, duplicate)
    )
    path = invalid / "duplicate-case.jsonl"
    atomic_write_bytes(path, (payload + "\n").encode("utf-8"))
    records["duplicate_case"] = str(path.relative_to(root))

    bad_lr = dict(first)
    bad_lr["lr"] = 0.0
    path = invalid / "bad-lr.jsonl"
    atomic_write_bytes(path, (canonical_bytes(bad_lr).decode("utf-8") + "\n").encode("utf-8"))
    records["bad_lr"] = str(path.relative_to(root))

    bad_micro = dict(first)
    bad_micro["steps"] = 3
    bad_micro["micro_batch"] = 2
    path = invalid / "bad-micro-batch.jsonl"
    atomic_write_bytes(path, (canonical_bytes(bad_micro).decode("utf-8") + "\n").encode("utf-8"))
    records["bad_micro_batch"] = str(path.relative_to(root))

    bad_version = dict(first)
    bad_version["task_version"] = "v9"
    path = invalid / "unknown-version.jsonl"
    atomic_write_bytes(path, (canonical_bytes(bad_version).decode("utf-8") + "\n").encode("utf-8"))
    records["unknown_version"] = str(path.relative_to(root))

    missing = dict(first)
    del missing["seed"]
    path = invalid / "missing-field.jsonl"
    atomic_write_bytes(path, (canonical_bytes(missing).decode("utf-8") + "\n").encode("utf-8"))
    records["missing_field"] = str(path.relative_to(root))

    bad_layer = dict(first)
    bad_layer["num_layers"] = bad_layer["num_layers"] + 1
    path = invalid / "bad-layer-count.jsonl"
    atomic_write_bytes(path, (canonical_bytes(bad_layer).decode("utf-8") + "\n").encode("utf-8"))
    records["bad_layer_count"] = str(path.relative_to(root))
    return records


def generate_dataset(
    output: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    task_count: int | None = None,
) -> dict[str, Any]:
    root = Path(output)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    blueprints: Iterable[dict[str, Any]] = CASE_BLUEPRINTS
    if task_count is not None:
        if task_count < 1 or task_count > len(CASE_BLUEPRINTS):
            raise ValueError(f"task_count must be in [1, {len(CASE_BLUEPRINTS)}]")
        blueprints = CASE_BLUEPRINTS[:task_count]
    task_dicts = [_task_dict(blueprint, seed) for blueprint in blueprints]
    tasks_path = root / "tasks.jsonl"
    atomic_write_bytes(
        tasks_path,
        ("\n".join(canonical_bytes(item).decode("utf-8") for item in task_dicts) + "\n").encode("utf-8"),
    )
    tasks = parse_tasks(tasks_path)
    snapshots_root = root / "snapshots"
    snapshots_root.mkdir()
    catalog: dict[str, Any] = {
        "catalog_version": "v1",
        "seed": seed,
        "time_origin_ms": TIME_ORIGIN_MS,
        "config_generation": 1,
        "tasks": {},
    }
    for task in tasks:
        snapshot = generate_snapshot(task, revision=task.revision)
        v1_path = snapshots_root / f"{task.case_id}.v1.json"
        v2_path = snapshots_root / f"{task.case_id}.v2"
        write_v1(snapshot, v1_path, provenance={"source": "fixed-seed-generator", "seed": seed})
        write_v2(snapshot, v2_path, provenance={"source": "fixed-seed-generator", "seed": seed})
        entry: dict[str, Any] = {
            "snapshot_id": snapshot.snapshot_id,
            "revision": task.revision,
            "seed": task.seed,
            "semantic_hash": snapshot.semantic_hash,
            "v1": str(v1_path.relative_to(root)),
            "v2": str(v2_path.relative_to(root)),
        }
        catalog["tasks"][task.case_id] = entry
    first_task = tasks[0]
    incremental_dir = snapshots_root / f"{first_task.case_id}.v2-incremental"
    shutil.copytree(snapshots_root / f"{first_task.case_id}.v2", incremental_dir)
    first_layer = first_task.layer_specs[0].key
    increment = append_v2_incremental_revision(incremental_dir, first_task, changed_layer_key=first_layer)
    catalog["tasks"][first_task.case_id]["incremental_v2"] = str(incremental_dir.relative_to(root))
    catalog["incremental_demo"] = {
        "case_id": first_task.case_id,
        "path": str(incremental_dir.relative_to(root)),
        **increment,
    }
    # Candidate snapshots for exercising a later revision without external services.
    revision_task = normalize_task(
        {
            **first_task.canonical_fields(),
            "revision": 1,
        }
    )
    candidate = generate_snapshot(
        revision_task,
        revision=1,
        generation=1,
        absolute_ms=revision_task.logical_clock_ms + 1000,
    )
    revision_root = root / "revisions"
    revision_root.mkdir()
    candidate_v2 = revision_root / f"{first_task.case_id}.r1.v2"
    candidate_v1 = revision_root / f"{first_task.case_id}.r1.v1.json"
    write_v2(candidate, candidate_v2, provenance={"source": "revision-candidate", "revision": 1}, generation=1)
    write_v1(candidate, candidate_v1, provenance={"source": "revision-candidate", "revision": 1})
    catalog["revision_candidates"] = {
        first_task.case_id: {
            "revision": 1,
            "v1": str(candidate_v1.relative_to(root)),
            "v2": str(candidate_v2.relative_to(root)),
            "semantic_hash": candidate.semantic_hash,
        }
    }
    catalog["invalid_samples"] = _write_invalid_samples(root, tasks)
    catalog_path = root / "catalog.json"
    atomic_write_bytes(catalog_path, canonical_bytes(catalog))
    return {
        "ok": True,
        "seed": seed,
        "output": str(root),
        "tasks_path": str(tasks_path),
        "catalog_path": str(catalog_path),
        "task_count": len(tasks),
        "task_list_sha256": sha256_bytes(tasks_path.read_bytes()),
        "sample_tree_digest": tree_digest(root),
        "catalog": catalog,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate fixed-seed PEFT diagnostic samples")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--task-count", type=int, default=None)
    args = parser.parse_args(argv)
    report = generate_dataset(args.output, seed=args.seed, task_count=args.task_count)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("ok", "seed", "output", "task_count", "task_list_sha256")
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

