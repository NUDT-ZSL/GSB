from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_json_bytes, pretty_json_bytes, sha256_json
from .config import _codebook_checksum
from .deterministic import deterministic_floats


EN_TOKENS = [
    "red",
    "blue",
    "green",
    "cat",
    "dog",
    "river",
    "sky",
    "stone",
    "warm",
    "cold",
    "bright",
    "quiet",
]
ZH_TOKENS = [
    "红色",
    "蓝色",
    "绿色",
    "小猫",
    "小狗",
    "河流",
    "天空",
    "石头",
    "温暖",
    "寒冷",
    "明亮",
    "安静",
]


def make_vocab() -> dict[str, Any]:
    languages = {}
    for lang, tokens in (("en", EN_TOKENS), ("zh", ZH_TOKENS)):
        weights = {
            token: round(0.75 + (index + 1) * 0.05, 6)
            for index, token in enumerate(tokens)
        }
        languages[lang] = {"weights": weights}
    vocab = {
        "schema": "cm-vocab-v1",
        "version": 1,
        "languages": languages,
    }
    vocab["checksum"] = sha256_json(
        {
            "schema": vocab["schema"],
            "languages": vocab["languages"],
        }
    )
    return vocab


def make_config() -> dict[str, Any]:
    return {
        "schema": "cm-pipeline-config-v1",
        "seed": 20260101,
        "image": {
            "shape": [24, 24, 3],
            "block_size": [4, 4],
            "projection_seed": 13001,
            "embed_dim": 16,
        },
        "text": {"embed_dim": 16},
        "align": {"dim": 16, "seed": 17001, "scale": 1.0},
        "index": {"num_shards": 3},
        "retrieval": {"top_k": 3, "metric": "cosine"},
        "graph": {
            "nodes": {
                "image_encoder": {"type": "image_encoder"},
                "text_encoder": {"type": "text_encoder"},
                "align": {"type": "align"},
                "index_build": {"type": "index_build"},
                "retrieve": {"type": "retrieve"},
            },
            "edges": [
                ["image_encoder", "align"],
                ["text_encoder", "align"],
                ["align", "index_build"],
                ["align", "retrieve"],
                ["index_build", "retrieve"],
            ],
            "entrypoint": "retrieve",
        },
        "resources": {
            "max_recompute_pairs": 10000,
            "max_codebook_bytes": 1048576,
            "max_cache_entries": 100000,
            "on_limit": "error",
        },
    }


def make_config_align_v2() -> dict[str, Any]:
    config = make_config()
    config["align"]["seed"] = 17002
    config["align"]["scale"] = 1.05
    return config


def _sample_text(index: int, lang: str) -> str:
    tokens = EN_TOKENS if lang == "en" else ZH_TOKENS
    positions = [
        index % len(tokens),
        (index * 3 + 2) % len(tokens),
        (index * 5 + 7) % len(tokens),
        (index * 7 + 1) % len(tokens),
        (index * 11 + 4) % len(tokens),
    ]
    return " ".join(tokens[position] for position in positions)


def make_samples(count: int = 12, *, updated: bool = False) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(count):
        pair_id = f"PAIR-{index + 1:04d}"
        lang = "zh" if index % 4 == 3 else "en"
        mask = ["image", "text"]
        if index == 9:
            mask = ["text"]
        elif index == 10:
            mask = ["image"]
        text = _sample_text(index, lang)
        revision = 1
        if updated and index == 1:
            text = _sample_text(index + 4, lang)
            revision = 2
        seed = 1000 + index * 37
        if updated and index == 4:
            seed = 900000 + index
        samples.append(
            {
                "pair_id": pair_id,
                "image_seed": seed,
                "image_shape": [24, 24, 3],
                "text": text,
                "text_lang": lang,
                "caption_revision": revision,
                "embed_dim": 16,
                "modality_mask": mask,
            }
        )
    return samples


def _make_codebook_blocks(version: int = 1) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block_id in range(3):
        values = deterministic_floats(
            f"fixture-codebook|version={version}|block={block_id}",
            4 * 16,
        ).reshape(4, 16)
        centroids = np.ascontiguousarray(values, dtype=np.float64)
        checksum = sha256_json(
            {
                "block_id": block_id,
                "centroids": centroids.tolist(),
            }
        )
        blocks.append(
            {
                "block_id": block_id,
                "version": version,
                "checksum": checksum,
                "centroids": centroids.tolist(),
            }
        )
    return blocks


def make_codebook(version: int = 1, *, evolve_block: int | None = None) -> dict[str, Any]:
    blocks = _make_codebook_blocks(version=1 if evolve_block is not None else version)
    if evolve_block is not None:
        target = blocks[evolve_block]
        values = np.asarray(target["centroids"], dtype=np.float64)
        values[0, 0] += 0.125
        target["centroids"] = values.tolist()
        target["version"] = 2
        target["checksum"] = sha256_json(
            {
                "block_id": evolve_block,
                "centroids": values.tolist(),
            }
        )
    codebook = {
        "schema": "cm-codebook-v1",
        "version": version,
        "dim": 16,
        "centroids_per_block": 4,
        "blocks": blocks,
    }
    codebook["checksum"] = _codebook_checksum(
        schema=codebook["schema"],
        dim=codebook["dim"],
        centroids_per_block=codebook["centroids_per_block"],
        blocks=blocks,
    )
    return codebook


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pretty_json_bytes(value))


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        for record in records:
            handle.write(canonical_json_bytes(record) + b"\n")


def write_invalid_fixtures(root: Path, samples: list[dict[str, Any]]) -> dict[str, Path]:
    invalid = root / "data" / "invalid"
    invalid.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    duplicate = [dict(item) for item in samples]
    duplicate.append(dict(samples[0]))
    paths["duplicate"] = invalid / "duplicate_pair.jsonl"
    _write_jsonl(paths["duplicate"], duplicate)

    seed_shape = [dict(item) for item in samples]
    seed_shape[2] = dict(seed_shape[2])
    seed_shape[2]["image_shape"] = [20, 24, 3]
    paths["seed_shape"] = invalid / "seed_shape_mismatch.jsonl"
    _write_jsonl(paths["seed_shape"], seed_shape)

    text_empty = [dict(item) for item in samples]
    text_empty[1] = dict(text_empty[1])
    text_empty[1]["text"] = "   "
    paths["text_empty"] = invalid / "text_empty.jsonl"
    _write_jsonl(paths["text_empty"], text_empty)

    mask_invalid = [dict(item) for item in samples]
    mask_invalid[3] = dict(mask_invalid[3])
    mask_invalid[3]["modality_mask"] = ["audio"]
    paths["mask_invalid"] = invalid / "mask_invalid.jsonl"
    _write_jsonl(paths["mask_invalid"], mask_invalid)

    embed_dim = [dict(item) for item in samples]
    embed_dim[4] = dict(embed_dim[4])
    embed_dim[4]["embed_dim"] = 0
    paths["embed_dim"] = invalid / "embed_dim_invalid.jsonl"
    _write_jsonl(paths["embed_dim"], embed_dim)

    combined = [dict(item) for item in samples]
    combined.append(dict(samples[0]))
    combined[2] = dict(combined[2])
    combined[2]["image_shape"] = [8, 8, 3]
    combined[5] = dict(combined[5])
    combined[5]["text"] = ""
    combined[7] = dict(combined[7])
    combined[7]["modality_mask"] = ["image", "audio"]
    combined[9] = dict(combined[9])
    combined[9]["embed_dim"] = -1
    paths["combined"] = invalid / "combined.jsonl"
    _write_jsonl(paths["combined"], combined)
    return paths


def write_fixtures(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    data = root / "data"
    config = root / "config"
    samples = make_samples(updated=False)
    samples_updated = make_samples(updated=True)
    vocab = make_vocab()
    pipeline = make_config()
    pipeline_align_v2 = make_config_align_v2()
    codebook_v1 = make_codebook(version=1)
    codebook_v2 = make_codebook(version=2, evolve_block=1)
    _write_jsonl(data / "samples.jsonl", samples)
    _write_jsonl(data / "samples_updated.jsonl", samples_updated)
    _write_json(config / "vocab.json", vocab)
    _write_json(config / "pipeline.json", pipeline)
    _write_json(config / "pipeline_align_v2.json", pipeline_align_v2)
    _write_json(config / "codebook_v1.json", codebook_v1)
    _write_json(config / "codebook_v2.json", codebook_v2)
    invalid_paths = write_invalid_fixtures(root, samples)
    return {
        "root": str(root),
        "samples": str(data / "samples.jsonl"),
        "samples_updated": str(data / "samples_updated.jsonl"),
        "vocab": str(config / "vocab.json"),
        "pipeline": str(config / "pipeline.json"),
        "pipeline_align_v2": str(config / "pipeline_align_v2.json"),
        "codebook_v1": str(config / "codebook_v1.json"),
        "codebook_v2": str(config / "codebook_v2.json"),
        "invalid": {name: str(path) for name, path in invalid_paths.items()},
    }


if __name__ == "__main__":
    print(json.dumps(write_fixtures(Path.cwd()), ensure_ascii=False, indent=2))

