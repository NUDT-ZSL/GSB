"""Local deterministic sample definitions used by the CLI and tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np

from .generator import (
    build_operands,
    densify_reference,
    operand_checksums,
    reference_checksum,
)
from .validation import parse_case_spec


def sample_definitions(base_seed: int = 20261119) -> list[dict[str, Any]]:
    return [
        {
            "case_id": "spmm_dense_7",
            "kind": "spmm",
            "shape_mnk": [72, 96, 7],
            "nnz_a": 520,
            "nnz_b": 96 * 7,
            "dtype": "float64",
            "seed": base_seed + 101,
            "block_candidates": [8, 12, 16, 24, 32, 48, 72],
            "b_storage": "dense",
            "generation": {
                "a_stream": 1,
                "d_stream": 3,
                "a_value_scale": 1.0,
                "b_value_scale": 0.75,
            },
        },
        {
            "case_id": "spspmm_balanced_40",
            "kind": "spspmm",
            "shape_mnk": [80, 96, 40],
            "nnz_a": 640,
            "nnz_b": 900,
            "dtype": "float64",
            "seed": base_seed + 211,
            "block_candidates": [8, 12, 16, 24, 32, 48, 80],
            "b_storage": "sparse",
            "generation": {
                "a_stream": 1,
                "b_stream": 2,
                "a_value_scale": 0.8,
                "b_value_scale": 0.6,
            },
        },
        {
            "case_id": "spspmm_batched_24",
            "kind": "spspmm",
            "shape_mnk": [2, 48, 64, 24],
            "nnz_a": 600,
            "nnz_b": 760,
            "dtype": "float64",
            "seed": base_seed + 307,
            "block_candidates": [8, 12, 16, 24, 48],
            "b_storage": "sparse",
            "generation": {
                "a_stream": 11,
                "b_stream": 12,
                "a_value_scale": 0.5,
                "b_value_scale": 0.9,
            },
        },
        {
            "case_id": "spspmm_aggregated_32",
            "kind": "spspmm",
            "shape_mnk": [64, 80, 32],
            "nnz_a": 420,
            "nnz_b": 260,
            "dtype": "float64",
            "seed": base_seed + 359,
            "block_candidates": [8, 12, 16, 24, 32, 64],
            "b_storage": "sparse",
            "generation": {
                "a_stream": 15,
                "b_stream": 16,
                "a_value_scale": 0.7,
                "b_value_scale": 0.4,
            },
        },
        {
            "case_id": "spmm_vector_1",
            "kind": "spmm",
            "shape_mnk": [64, 128, 1],
            "nnz_a": 460,
            "nnz_b": 128,
            "dtype": "float64",
            "seed": base_seed + 401,
            "block_candidates": [8, 16, 32, 64],
            "b_storage": "dense",
            "generation": {
                "a_stream": 21,
                "d_stream": 23,
                "a_value_scale": 1.0,
                "b_value_scale": 0.25,
            },
        },
        {
            "case_id": "spspmm_float32_mini",
            "kind": "spspmm",
            "shape_mnk": [40, 56, 7],
            "nnz_a": 240,
            "nnz_b": 300,
            "dtype": "float32",
            "seed": base_seed + 503,
            "block_candidates": [8, 14, 28, 40],
            "b_storage": "sparse",
            "generation": {
                "a_stream": 31,
                "b_stream": 32,
                "a_value_scale": 0.5,
                "b_value_scale": 0.5,
                "subnormal_fraction": 0.01,
                "subnormal_scale": 1e-44,
            },
        },
    ]


def finalize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    finalized, _ = _finalize_with_reference(metadata)
    return finalized


def _finalize_with_reference(
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], np.ndarray]:
    spec = parse_case_spec(metadata)
    operands = build_operands(spec)
    reference = densify_reference(spec, operands)
    finalized = copy.deepcopy(metadata)
    checksums = operand_checksums(spec, operands)
    checksums["reference"] = reference_checksum(reference)
    finalized["checksums"] = checksums
    return finalized, reference


def write_samples(
    output_directory: str | Path,
    *,
    base_seed: int = 20261119,
) -> dict[str, Any]:
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    for definition in sample_definitions(base_seed):
        metadata, reference = _finalize_with_reference(definition)
        filename = f"case_{metadata['case_id']}.json"
        path = directory / filename
        path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        reference_filename = f"reference_{metadata['case_id']}.npy"
        np.save(directory / reference_filename, reference, allow_pickle=False)
        cases.append(
            {
                "case_id": metadata["case_id"],
                "file": filename,
                "reference_file": reference_filename,
                "kind": metadata["kind"],
                "dtype": metadata["dtype"],
            }
        )
    manifest = {
        "schema_version": "1.0",
        "base_seed": base_seed,
        "generator": "numpy.PCG64 + SeedSequence, canonical sorted CSR",
        "cases": cases,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_sample_files(directory: str | Path) -> list[dict[str, Any]]:
    root = Path(directory)
    files = sorted(root.glob("case_*.json"))
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in files
    ]

