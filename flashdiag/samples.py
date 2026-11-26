"""Fixed-seed JSONL and NumPy sample generation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .hashing import array_sha256, canonical_json_text, sha256_json


@dataclass(frozen=True)
class SampleCase:
    case_id: str
    seq_len: int
    head_dim: int
    num_heads: int
    q_block_len: int
    kv_block_len: int
    dtype: str
    causal: bool
    seed: int
    changed_head: int
    changed_from: int


DEFAULT_SAMPLE_CASES: tuple[SampleCase, ...] = (
    SampleCase(
        case_id="causal-q16-kv16",
        seq_len=64,
        head_dim=16,
        num_heads=2,
        q_block_len=16,
        kv_block_len=16,
        dtype="float64",
        causal=True,
        seed=101,
        changed_head=1,
        changed_from=33,
    ),
    SampleCase(
        case_id="noncausal-q12-kv8",
        seq_len=48,
        head_dim=8,
        num_heads=3,
        q_block_len=12,
        kv_block_len=8,
        dtype="float32",
        causal=False,
        seed=202,
        changed_head=0,
        changed_from=8,
    ),
)


def _rng_for(seed: int, stream: int, *extra: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([seed, stream, *extra]))


def _base_arrays(case: SampleCase) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = _rng_for(case.seed, 0xFA11)
    shape = (case.num_heads, case.seq_len, case.head_dim)
    q = rng.standard_normal(shape, dtype=np.float64)
    k = rng.standard_normal(shape, dtype=np.float64)
    v = rng.standard_normal(shape, dtype=np.float64)
    dtype = np.dtype(case.dtype)
    return q.astype(dtype), k.astype(dtype), v.astype(dtype)


def _revision_arrays(
    case: SampleCase,
    base: tuple[np.ndarray, np.ndarray, np.ndarray],
    revision: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    q, k, v = (array.copy() for array in base)
    start = case.changed_from
    shape = (case.seq_len - start, case.head_dim)
    k_rng = _rng_for(case.seed, 0xBEEF, revision, case.changed_head, 1)
    v_rng = _rng_for(case.seed, 0xBEEF, revision, case.changed_head, 2)
    k[case.changed_head, start:, :] += k_rng.standard_normal(shape, dtype=np.float64).astype(
        k.dtype
    ) * 0.35
    v[case.changed_head, start:, :] += v_rng.standard_normal(shape, dtype=np.float64).astype(
        v.dtype
    ) * 0.25
    return q, k, v


def _task_record(
    case: SampleCase,
    revision: int,
    array_dir: Path,
    data_root: Path,
    role: str,
) -> dict[str, Any]:
    relative_dir = array_dir.relative_to(data_root)
    return {
        "case_id": case.case_id,
        "seq_len": case.seq_len,
        "head_dim": case.head_dim,
        "num_heads": case.num_heads,
        "q_block_len": case.q_block_len,
        "kv_block_len": case.kv_block_len,
        "weight_revision": revision,
        "dtype": case.dtype,
        "causal": case.causal,
        "seed": case.seed,
        "q_path": str(relative_dir / "q.npy"),
        "k_path": str(relative_dir / "k.npy"),
        "v_path": str(relative_dir / "v.npy"),
        "sample_role": role,
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(canonical_json_text(record) + "\n" for record in records)
    path.write_text(text, encoding="utf-8")


def generate_samples(
    data_root: str | Path,
    cases: tuple[SampleCase, ...] = DEFAULT_SAMPLE_CASES,
    revision: int = 2,
) -> dict[str, Any]:
    """Generate deterministic base/revision manifests and Q/K/V arrays.

    The same arguments always produce byte-identical array files and JSONL
    manifests. Revision arrays change K/V for exactly one head per case.
    """

    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    base_records: list[dict[str, Any]] = []
    revision_records: list[dict[str, Any]] = []
    file_hashes: dict[str, str] = {}
    array_hashes: dict[str, dict[str, str]] = {}
    changed_heads: dict[str, int] = {}

    for case in cases:
        if revision <= 1:
            raise ValueError("revision must be >= 2")
        if not 0 <= case.changed_head < case.num_heads:
            raise ValueError(f"changed_head out of range for {case.case_id}")
        if not 0 <= case.changed_from < case.seq_len:
            raise ValueError(f"changed_from out of range for {case.case_id}")

        base = _base_arrays(case)
        revised = _revision_arrays(case, base, revision)
        for rev, arrays, role, target in (
            (1, base, "base", base_records),
            (revision, revised, "revision", revision_records),
        ):
            array_dir = root / "arrays" / case.case_id / f"rev{rev}"
            array_dir.mkdir(parents=True, exist_ok=True)
            record = _task_record(case, rev, array_dir, root, role)
            target.append(record)
            hashes: dict[str, str] = {}
            for name, array in zip(("q", "k", "v"), arrays, strict=True):
                path = array_dir / f"{name}.npy"
                np.save(path, array, allow_pickle=False)
                relative = str(path.relative_to(root))
                file_hashes[relative] = array_sha256(array)
                hashes[name] = file_hashes[relative]
            array_hashes[f"{case.case_id}/rev{rev}"] = hashes
        changed_heads[case.case_id] = case.changed_head

    base_manifest = root / "tasks.jsonl"
    revision_manifest = root / "tasks_rev.jsonl"
    _write_jsonl(base_manifest, base_records)
    _write_jsonl(revision_manifest, revision_records)

    summary_payload = {
        "generator": "flashdiag.samples",
        "version": 1,
        "base_manifest": base_manifest.name,
        "revision_manifest": revision_manifest.name,
        "revision": revision,
        "cases": [
            {
                "case_id": case.case_id,
                "changed_head": case.changed_head,
                "changed_from": case.changed_from,
            }
            for case in cases
        ],
        "changed_heads": changed_heads,
        "array_hashes": array_hashes,
        "file_hashes": dict(sorted(file_hashes.items())),
    }
    summary_payload["generation_sha256"] = sha256_json(summary_payload)
    summary_path = root / "generation_manifest.json"
    summary_path.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "data_root": str(root),
        "base_manifest": str(base_manifest),
        "revision_manifest": str(revision_manifest),
        "summary": str(summary_path),
        "generation_sha256": summary_payload["generation_sha256"],
        "cases": len(cases),
    }


def tree_content_sha256(root: str | Path) -> str:
    """Hash a generated tree's relative paths and file bytes deterministically."""

    import hashlib

    base = Path(root)
    digest = hashlib.sha256()
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(path.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()

