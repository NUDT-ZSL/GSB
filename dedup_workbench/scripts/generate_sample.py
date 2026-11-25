#!/usr/bin/env python3
"""Generate deterministic local JSONL shards and dedup configuration."""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dedup_workbench.ingest import content_hash_for  # noqa: E402


SEED = 20261126
CHUNK_LENGTH = 32


def fixed_block(prefix: str, index: int, length: int = CHUNK_LENGTH) -> str:
    stem = f"{prefix}-{index:02d}-"
    if len(stem) > length:
        raise ValueError("block prefix is too long")
    filler = "0123456789abcdef"
    suffix = "".join(filler[(index + offset) % len(filler)] for offset in range(length - len(stem)))
    return stem + suffix


def build_records() -> list[dict[str, object]]:
    rng = random.Random(SEED)
    # The main sample keeps duplicate groups independent.  Window exclusion is
    # covered by deterministic synthetic tests with an oversized hash bucket.
    common_block = ""
    topics = [
        ("alpha", "en", "wiki"),
        ("beta", "zh", "official"),
        ("gamma", "code", "reference"),
        ("delta", "en", "article"),
        ("epsilon", "zh", "book"),
        ("zeta", "code", "web"),
    ]

    records: list[dict[str, object]] = []
    serial = 1

    def add_record(
        *,
        shard_index: int,
        text: str,
        lang: str,
        source: str,
        quality: float,
        token_estimate: int,
    ) -> str:
        nonlocal serial
        doc_id = f"doc-{serial:04d}"
        serial += 1
        records.append(
            {
                "shard_id": f"shard-{shard_index:03d}",
                "doc_id": doc_id,
                "text": text,
                "lang": lang,
                "source": source,
                "token_estimate": token_estimate,
                "quality_score": quality,
                "content_hash": content_hash_for(text),
            }
        )
        return doc_id

    for topic_index, (topic, lang, source) in enumerate(topics):
        base_blocks = [fixed_block(f"{topic}-signature", block_index) for block_index in range(8)]
        base_text = common_block + "".join(base_blocks)
        token_base = 180 + topic_index * 37

        add_record(
            shard_index=topic_index % 4,
            text=base_text,
            lang=lang,
            source=source,
            quality=0.72 + topic_index * 0.015,
            token_estimate=token_base,
        )
        add_record(
            shard_index=(topic_index + 1) % 4,
            text=base_text,
            lang=lang,
            source="official" if topic_index % 2 == 0 else "reference",
            quality=0.97 if topic_index % 2 == 0 else 0.91,
            token_estimate=token_base + rng.randint(2, 18),
        )

        near_blocks = list(base_blocks)
        near_blocks[5] = fixed_block(f"{topic}-revised", 50 + topic_index)
        near_text = common_block + "".join(near_blocks)
        add_record(
            shard_index=(topic_index + 2) % 4,
            text=near_text,
            lang=lang,
            source="article" if lang != "code" else "web",
            quality=0.61 + topic_index * 0.01,
            token_estimate=token_base + rng.randint(-8, 24),
        )

        if topic_index % 2 == 1:
            add_record(
                shard_index=(topic_index + 3) % 4,
                text=base_text,
                lang=lang,
                source="crawl",
                quality=0.34,
                token_estimate=token_base + rng.randint(-20, 40),
            )

    for extra_index in range(4):
        blocks = [fixed_block(f"solo-{extra_index}", block_index) for block_index in range(6)]
        text = common_block + "".join(blocks)
        add_record(
            shard_index=extra_index,
            text=text,
            lang=("en", "zh", "code", "multi")[extra_index],
            source=("web", "crawl", "reference", "article")[extra_index],
            quality=(0.52, 0.39, 0.86, 0.70)[extra_index],
            token_estimate=90 + extra_index * 31,
        )

    return records


def build_config() -> dict[str, object]:
    return {
        "config_version": 1,
        "chunk_signature_length": CHUNK_LENGTH,
        "similarity_threshold": 0.75,
        "candidate_window": 4,
        "validation_mode": "full",
        "sampling_seed": SEED,
        "bucket_strategy": "anchor_digest",
        "bucket_anchor_count": 3,
        "bucket_prefix_chars": 16,
        "allowed_langs": ["zh", "en", "ja", "code", "multi"],
        "limits": {
            "max_shards": 8,
            "max_blocks_per_doc": 32,
            "max_cache_memory_bytes": 4 * 1024 * 1024,
            "latency_budget_ms": 25.0,
            "query_loop_iterations": 41,
            "batch_size": 5,
            "cache_overflow_policy": "evict_then_spill",
        },
        "latency_model": {
            "base_ms": 0.02,
            "per_block_ms": 0.001,
            "per_comparison_ms": 0.003,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "samples")
    args = parser.parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    records = build_records()
    jsonl_path = output_dir / "corpus_shards.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")

    config_path = output_dir / "dedup_config.json"
    config_path.write_text(json.dumps(build_config(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "seed": SEED,
                "record_count": len(records),
                "jsonl_path": str(jsonl_path),
                "config_path": str(config_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
