from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .models import AttentionCase, QKVArrays
from .validation import load_cases_jsonl


def array_paths(samples_dir: str | Path, case_id: str) -> tuple[Path, Path, Path]:
    root = Path(samples_dir) / "arrays"
    safe_id = case_id.replace("/", "_").replace("\\", "_")
    return (
        root / f"{safe_id}__q.npy",
        root / f"{safe_id}__k.npy",
        root / f"{safe_id}__v.npy",
    )


def load_sample_arrays(
    samples_dir: str | Path,
    case: AttentionCase,
) -> QKVArrays:
    q_path, k_path, v_path = array_paths(samples_dir, case.case_id)
    return QKVArrays.load_npy(q_path, k_path, v_path)


def load_samples(
    samples_dir: str | Path,
) -> list[tuple[AttentionCase, QKVArrays]]:
    root = Path(samples_dir)
    cases = load_cases_jsonl(root / "cases.jsonl")
    return [(case, load_sample_arrays(root, case)) for case in cases]


def write_manifest(path: str | Path, payload: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
