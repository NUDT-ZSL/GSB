from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_json_bytes, checksum, sha256_bytes


USER_FEATURES: tuple[tuple[int, int], ...] = (
    (0, 1),
    (1, 2),
    (2, 1),
    (3, 1),
    (4, 2),
    (5, 1),
)
ITEM_FEATURES: tuple[tuple[int, int], ...] = (
    (100, 1),
    (101, 1),
    (102, 2),
    (103, 1),
    (104, 1),
    (105, 2),
)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _feature_lines(dictionary_version: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature_index, dimension in USER_FEATURES:
        rows.append(
            {
                "cardinality": 17 + feature_index,
                "dictionary_version": dictionary_version,
                "dimension": dimension,
                "feature_index": feature_index,
                "type": "user",
            }
        )
    for feature_index, dimension in ITEM_FEATURES:
        rows.append(
            {
                "cardinality": 29 + feature_index - 100,
                "dictionary_version": dictionary_version,
                "dimension": dimension,
                "feature_index": feature_index,
                "type": "item",
            }
        )
    return rows


def _draw_matrix(
    rng: random.Random, specs: tuple[tuple[int, int], ...]
) -> tuple[list[int], list[list[float]], dict[int, list[float]]]:
    indices = [feature_index for feature_index, _ in specs]
    values: list[list[float]] = []
    by_index: dict[int, list[float]] = {}
    for feature_index, dimension in specs:
        vector = [round(rng.gauss(0.0, 0.8), 10) for _ in range(dimension)]
        values.append(vector)
        by_index[feature_index] = vector
    return indices, values, by_index


def generate_dataset(
    out_dir: str | Path,
    *,
    seed: int = 20261121,
    sample_count: int = 120,
    dictionary_version: str = "dict-v1",
) -> dict[str, Any]:
    if sample_count <= 0:
        raise ValueError("sample_count must be positive")
    if not dictionary_version:
        raise ValueError("dictionary_version must not be empty")

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    dictionary_rows = _feature_lines(dictionary_version)
    dictionary_rows.sort(key=lambda row: row["feature_index"])
    dictionary_bytes = b"".join(
        canonical_json_bytes(row) + b"\n" for row in dictionary_rows
    )
    dictionary_checksum = sha256_bytes(dictionary_bytes)

    sample_rows: list[dict[str, Any]] = []
    for offset in range(sample_count):
        user_indices, user_values, user_by_id = _draw_matrix(rng, USER_FEATURES)
        item_indices, item_values, item_by_id = _draw_matrix(rng, ITEM_FEATURES)

        cross_slots = [
            {"left": 0, "right": 100, "slot": 0},
            {"left": 1, "right": 102, "slot": 1},
        ]
        if offset % 4 == 3:
            cross_slots = cross_slots[:1]

        logit = (
            1.35 * user_by_id[0][0] * item_by_id[100][0]
            + 0.85
            * (
                user_by_id[1][0] * item_by_id[102][0]
                + user_by_id[1][1] * item_by_id[102][1]
            )
            + 0.35 * user_by_id[2][0]
            + 0.25 * item_by_id[101][0]
            - 0.20 * user_by_id[3][0]
            + 0.15 * user_by_id[4][1]
        )
        probability = _sigmoid(logit)
        label = 1 if rng.random() < probability else 0
        sample_rows.append(
            {
                "cross_slots": cross_slots,
                "item_indices": item_indices,
                "item_values": [
                    [round(value, 10) for value in vector] for vector in item_values
                ],
                "label": label,
                "sample_id": f"s{offset + 1:06d}",
                "user_indices": user_indices,
                "user_values": [
                    [round(value, 10) for value in vector] for vector in user_values
                ],
            }
        )

    sample_rows.sort(key=lambda row: row["sample_id"])
    sample_bytes = b"".join(
        canonical_json_bytes(row) + b"\n" for row in sample_rows
    )
    sample_checksum = sha256_bytes(sample_bytes)

    exposure_path = output / "exposures.jsonl"
    dictionary_path = output / "features.jsonl"
    atomic_write_bytes(exposure_path, sample_bytes)
    atomic_write_bytes(dictionary_path, dictionary_bytes)

    manifest: dict[str, Any] = {
        "dictionary_checksum": dictionary_checksum,
        "dictionary_version": dictionary_version,
        "exposures_checksum": sample_checksum,
        "exposures_file": exposure_path.name,
        "features_file": dictionary_path.name,
        "generator": "ctr_pipeline.generator.v1",
        "sample_count": len(sample_rows),
        "seed": seed,
    }
    manifest["manifest_checksum"] = checksum(manifest)
    atomic_write_bytes(
        output / "manifest.json", canonical_json_bytes(manifest) + b"\n"
    )
    return manifest


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> str:
    data = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    atomic_write_bytes(path, data)
    return sha256_bytes(data)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic CTR samples")
    parser.add_argument("--out", default="data")
    parser.add_argument("--seed", type=int, default=20261121)
    parser.add_argument("--samples", type=int, default=120)
    parser.add_argument("--dict-version", default="dict-v1")
    args = parser.parse_args(argv)
    manifest = generate_dataset(
        args.out,
        seed=args.seed,
        sample_count=args.samples,
        dictionary_version=args.dict_version,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
