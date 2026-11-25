"""Interactive retrieval and export of committed case products."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .cache import ArtifactStore
from .configs import ConfigStore
from .errors import EConfigInvalid, ELedgerCorrupt, ETaskInvalid
from .ledger import VersionedLedger
from .models import Task
from .util import sha256_obj


def _array_summary(array: np.ndarray) -> dict[str, Any]:
    digest = hashlib.sha256(array.tobytes(order="C")).hexdigest()
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "digest": digest,
        "min": round(float(np.min(array)), 12),
        "max": round(float(np.max(array)), 12),
    }


def _atomic_savez(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as handle:
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def query_case(
    output_dir: str | Path,
    case_id: str,
    *,
    mode: str = "both",
    export_npz: str | Path | None = None,
    input_dir: str | Path | None = None,
) -> dict[str, Any]:
    if mode not in {"train", "infer", "both"}:
        raise ValueError("query mode must be train, infer, or both")
    output_dir = Path(output_dir)
    ledger_dir = output_dir / "ledgers"
    progress_ledger = VersionedLedger(ledger_dir, "progress")
    cache_ledger = VersionedLedger(ledger_dir, "stage_cache")
    sample_ledger = VersionedLedger(ledger_dir, "samples")
    task_ledger = VersionedLedger(ledger_dir, "tasks")
    config_ledger = VersionedLedger(ledger_dir, "configs")
    progress_snapshot = progress_ledger.read()
    cache_snapshot = cache_ledger.read()
    sample_snapshot = sample_ledger.read()
    task_snapshot = task_ledger.read()
    config_snapshot = config_ledger.read()
    if any(
        item is None
        for item in (
            progress_snapshot,
            cache_snapshot,
            sample_snapshot,
            task_snapshot,
            config_snapshot,
        )
    ):
        raise ETaskInvalid("output workspace has incomplete ledgers", {"output_dir": str(output_dir)})

    checkpoint_mode: str | None = None
    suffix_stage = None if mode == "both" else (
        "train_backward" if mode == "train" else "inference"
    )
    candidates = ["both", mode] if mode != "both" else ["both", "train", "infer"]
    for candidate in candidates:
        checkpoint = progress_snapshot["data"]["checkpoints"].get(candidate)
        if not checkpoint or case_id not in checkpoint.get("completed", {}):
            continue
        entry = checkpoint["completed"][case_id]
        if suffix_stage is None or suffix_stage in entry.get("stages", []):
            checkpoint_mode = candidate
            break
    if checkpoint_mode is None:
        raise ETaskInvalid("case is not committed in requested mode", {"case_id": case_id})

    entry = progress_snapshot["data"]["checkpoints"][checkpoint_mode]["completed"][case_id]
    task_rows = {
        row["case_id"]: row for row in task_snapshot["data"]["tasks"]
    }
    if case_id not in task_rows:
        raise ETaskInvalid("case is absent from task ledger", {"case_id": case_id})
    task = Task.from_dict(task_rows[case_id])
    if input_dir is None:
        input_dir = output_dir.parent / "input"
    input_dir = Path(input_dir)
    config_store = ConfigStore(
        input_dir / "configs" / "config_v1.json",
        input_dir / "configs" / "v2",
    )
    try:
        resolved = config_store.resolve_at(task.logical_clock_ms)
    except EConfigInvalid:
        raise
    if resolved.generation != entry["config_generation"] or resolved.config_hash != entry["config_hash"]:
        raise ELedgerCorrupt(
            "committed entry config does not resolve from current snapshots",
            {
                "case_id": case_id,
                "committed_generation": entry["config_generation"],
                "resolved_generation": resolved.generation,
            },
        )

    store = ArtifactStore(output_dir / "stage_cache")
    index = cache_snapshot["data"]["index"]
    sample_entry = sample_snapshot["data"]["samples"].get(case_id, {})
    bundles: dict[str, dict[str, np.ndarray]] = {}
    summaries: dict[str, Any] = {}
    stage_names = {
        "train": ("sample", "quantize", "model", "forward", "train_backward"),
        "infer": ("sample", "quantize", "model", "forward", "inference"),
    }
    requested_modes = (
        [
            selected
            for selected in ("train", "infer")
            if (
                "train_backward" if selected == "train" else "inference"
            )
            in entry["stages"]
        ]
        if mode == "both"
        else [mode]
    )
    if not requested_modes:
        raise ETaskInvalid(
            "case has no committed product for requested mode",
            {"case_id": case_id, "mode": mode},
        )
    for selected in requested_modes:
        suffix_stage = "train_backward" if selected == "train" else "inference"
        if suffix_stage not in entry["stages"]:
            raise ETaskInvalid(
                "case has no committed product for requested mode",
                {"case_id": case_id, "mode": selected},
            )
        bundle: dict[str, np.ndarray] = {}
        selected_summaries: dict[str, Any] = {}
        for stage in stage_names[selected]:
            key = entry["keys"][stage]
            if key not in index:
                raise ELedgerCorrupt(
                    "committed cache key is absent from cache index",
                    {"case_id": case_id, "stage": stage, "key": key},
                )
            arrays = store.load(index[key])
            for name, array in arrays.items():
                bundle[f"{selected}_{name}"] = array
            selected_summaries[stage] = {
                "cache_key": key,
                "metadata": index[key].get("metadata", {}),
                "arrays": {name: _array_summary(array) for name, array in arrays.items()},
            }
        bundles[selected] = bundle
        summaries[selected] = selected_summaries

    all_arrays: dict[str, np.ndarray] = {}
    for selected, bundle in bundles.items():
        all_arrays.update(bundle)
    export_path: str | None = None
    if export_npz is not None:
        export_path = str(Path(export_npz))
        _atomic_savez(Path(export_npz), all_arrays)

    result = {
        "case_id": case_id,
        "requested_mode": mode,
        "checkpoint_mode": checkpoint_mode,
        "checkpoint_version": progress_snapshot["data"]["checkpoints"][checkpoint_mode]["version"],
        "task": task.to_dict(),
        "task_content_hash": task.content_hash,
        "config": {
            "generation": resolved.generation,
            "effective_at_ms": resolved.effective_at_ms,
            "config_hash": resolved.config_hash,
            "source": resolved.source,
        },
        "data_summary": entry.get("sample_summary", sample_entry),
        "modes": summaries,
        "available_modes": sorted(bundles),
        "export_npz": export_path,
        "query_checksum": sha256_obj(
            {
                "case_id": case_id,
                "task_content_hash": task.content_hash,
                "config_hash": resolved.config_hash,
                "keys": entry["keys"],
                "summaries": summaries,
            }
        ),
    }
    return result
