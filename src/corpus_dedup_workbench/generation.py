from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .signatures import build_signature_record
from .util import canonical_json

DEFAULT_SEED = 20261123
DEFAULT_SHARDS = 4
DEFAULT_DOCS_PER_SHARD = 18
TOKENS_PER_DOC = 192
SIGNATURE_LENGTH = 12


def default_config_mapping(
    seed: int = DEFAULT_SEED,
    shards: int = DEFAULT_SHARDS,
    docs_per_shard: int = DEFAULT_DOCS_PER_SHARD,
) -> dict[str, Any]:
    return {
        "signature_length": SIGNATURE_LENGTH,
        "similarity_threshold": "0.75",
        "candidate_window": 4,
        "validation_mode": "full",
        "seed": seed,
        "threshold_policy": "tolerance_band",
        "threshold_tolerance": "0.000000001",
        "summary_bands": 4,
        "values_per_band": 2,
        "digest_min_common_bands": 2,
        "normalization_version": "nfkc-casefold-token-v1",
        "max_docs_per_batch": 256,
        "max_candidate_comparisons": 5000,
        "max_cache_entries": 128,
        "latency_budget_ms": 50.0,
        "resource_policy": "error",
        "sample": {
            "shards": shards,
            "docs_per_shard": docs_per_shard,
            "tokens_per_document": TOKENS_PER_DOC,
            "seed": seed,
        },
    }


def _vocabulary(rng: np.random.Generator) -> list[str]:
    prefixes = ["sx", "qv", "lm", "nr", "tp", "ka", "zu", "we"]
    suffixes = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "m"]
    return [
        f"{prefix}{suffix}{int(index):02d}"
        for index, (prefix, suffix) in enumerate(
            (prefix, suffix)
            for prefix in prefixes
            for suffix in suffixes
        )
    ]


def _tokens_text(tokens: list[str]) -> str:
    return " ".join(tokens)


def _family_variants(
    base_tokens: list[str],
    config: WorkbenchConfig,
    family_index: int,
) -> list[tuple[list[str], float, str]]:
    base_doc = {
        "doc_id": f"probe-{family_index}",
        "shard_id": "probe",
        "text": _tokens_text(base_tokens),
    }
    base_signature = build_signature_record(base_doc, config)
    descending_positions = [
        block["position"]
        for block in sorted(
            base_signature["blocks"],
            key=lambda block: int(block["signature"], 16),
            reverse=True,
        )
    ]

    def mutate(positions: list[int], tag: str) -> list[str]:
        for attempt in range(256):
            candidate = list(base_tokens)
            for order, position in enumerate(positions):
                token_index = min(
                    len(candidate) - 1,
                    position * config.signature_length + (order % config.signature_length),
                )
                candidate[token_index] = (
                    f"mut{tag}{family_index}{attempt}{order}{token_index}"
                )
            candidate_signature = build_signature_record(
                {
                    "doc_id": f"probe-{family_index}",
                    "shard_id": "probe",
                    "text": _tokens_text(candidate),
                },
                config,
            )
            if (
                candidate_signature["summary"]["bands"][0]
                == base_signature["summary"]["bands"][0]
            ):
                return candidate
        raise RuntimeError("无法生成保持候选桶稳定的确定性变体")

    one_change = mutate(descending_positions[:1], "one")
    three_change = mutate(descending_positions[:3], "three")
    four_change = mutate(descending_positions[:4], "four")
    return [
        (list(base_tokens), 78.0, "base"),
        (one_change, 91.5, "one_block_change"),
        (three_change, 84.25, "three_block_change"),
        (four_change, 72.0, "threshold_boundary_four_block_change"),
    ]


def generate_sample(
    output_dir: str | Path,
    config_path: str | Path,
    seed: int = DEFAULT_SEED,
    shards: int = DEFAULT_SHARDS,
    docs_per_shard: int = DEFAULT_DOCS_PER_SHARD,
) -> dict[str, Any]:
    if shards <= 0 or docs_per_shard <= 0:
        raise ValueError("shards 和 docs_per_shard 必须为正数")
    if docs_per_shard < 1:
        raise ValueError("每个分片至少需要一篇文档")
    output_dir = Path(output_dir)
    config_path = Path(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config_mapping = default_config_mapping(seed, shards, docs_per_shard)
    config = WorkbenchConfig.from_mapping(config_mapping)
    config_path.write_text(
        json.dumps(config_mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rng = np.random.Generator(np.random.PCG64(seed))
    vocab = _vocabulary(rng)
    docs: list[dict[str, Any]] = []
    doc_counter = 0
    family_records: list[dict[str, Any]] = []
    for shard_index in range(shards):
        shard_id = f"shard-{shard_index:02d}"
        shard_docs: list[dict[str, Any]] = []
        target_families = max(1, docs_per_shard // 6)
        family_count = 0
        while (
            len(shard_docs) + 4 <= docs_per_shard
            and family_count < target_families
        ):
            base_indices = rng.integers(0, len(vocab), size=TOKENS_PER_DOC)
            base_tokens = [vocab[int(index)] for index in base_indices]
            variants = _family_variants(base_tokens, config, family_count)
            family_doc_ids: list[str] = []
            for variant_index, (tokens, quality, _) in enumerate(variants):
                doc_id = f"doc-{doc_counter:06d}"
                doc_counter += 1
                family_doc_ids.append(doc_id)
                shard_docs.append(
                    {
                        "shard_id": shard_id,
                        "doc_id": doc_id,
                        "text": _tokens_text(tokens),
                        "lang": "en" if variant_index % 2 == 0 else "zh",
                        "source": "synthetic/numpy-pcg64",
                        "token_estimate": len(tokens),
                        "quality_score": quality,
                    }
                )
            family_records.append(
                {
                    "shard_id": shard_id,
                    "family_index": family_count,
                    "doc_ids": family_doc_ids,
                    "variants": [
                        {
                            "doc_id": family_doc_ids[index],
                            "name": variant[2],
                            "quality_score": variant[1],
                        }
                        for index, variant in enumerate(variants)
                    ],
                }
            )
            family_count += 1

        while len(shard_docs) < docs_per_shard:
            indices = rng.integers(0, len(vocab), size=TOKENS_PER_DOC)
            tokens = [vocab[int(index)] for index in indices]
            doc_id = f"doc-{doc_counter:06d}"
            doc_counter += 1
            shard_docs.append(
                {
                    "shard_id": shard_id,
                    "doc_id": doc_id,
                    "text": _tokens_text(tokens),
                    "lang": "en" if doc_counter % 3 else "zh",
                    "source": "synthetic/numpy-pcg64",
                    "token_estimate": len(tokens),
                    "quality_score": round(
                        40.0 + float(rng.random()) * 40.0, 6
                    ),
                }
            )
        docs.extend(shard_docs)

    manifest_path = output_dir / "manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        for doc in docs:
            handle.write(canonical_json(doc) + "\n")

    metadata = {
        "generator": "numpy.PCG64",
        "seed": seed,
        "shards": shards,
        "docs_per_shard": docs_per_shard,
        "doc_count": len(docs),
        "tokens_per_document": TOKENS_PER_DOC,
        "signature_length": SIGNATURE_LENGTH,
        "config_file": str(config_path),
        "manifest_file": str(manifest_path),
        "families": family_records,
        "note": "全部 token 和文档均为本地确定性合成，不含真实语料。",
    }
    (output_dir / "sample_meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "manifest_path": str(manifest_path),
        "config_path": str(config_path),
        "doc_count": len(docs),
        "shards": shards,
        "docs_per_shard": docs_per_shard,
        "seed": seed,
        "sample_meta_path": str(output_dir / "sample_meta.json"),
    }

