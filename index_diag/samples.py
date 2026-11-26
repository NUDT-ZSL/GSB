"""Fixed-seed deterministic sample generation for local verification."""

from __future__ import annotations

import random
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .config import upgrade_v1_to_v2
from .util import atomic_write_json, canonical_json, sha256_json


VOCABULARY = [
    "search",
    "recommend",
    "index",
    "query",
    "ranking",
    "rerank",
    "vector",
    "recall",
    "filter",
    "cache",
    "shard",
    "ledger",
    "checkpoint",
    "compress",
    "delta",
    "posting",
    "document",
    "batch",
    "snapshot",
    "recover",
]


def _document(
    rng: random.Random,
    *,
    batch_no: int,
    doc_id: int,
    forced_terms: list[str] | None = None,
) -> dict[str, Any]:
    if forced_terms is None:
        length = rng.randint(7, 11)
        terms = [rng.choice(VOCABULARY) for _ in range(length)]
        # Force at least one term to occur twice so position lists are meaningful.
        terms.append(terms[0])
    else:
        terms = list(forced_terms)
    frequencies = Counter(terms)
    positions: dict[str, list[int]] = {}
    for position, term in enumerate(terms):
        positions.setdefault(term, []).append(position)
    body = {
        "batch_no": batch_no,
        "doc_id": doc_id,
        "doc_length": len(terms),
        "term_frequencies": dict(sorted(frequencies.items())),
        "positions": dict(sorted(positions.items())),
        "terms": terms,
    }
    body["content_hash"] = sha256_json(body)
    return body


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
    path.write_bytes(data)
    return sha256_json(list(rows))


def _write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def _base_config(*, generation: int, chunk_size: int = 4) -> dict[str, Any]:
    return {
        "version": 1,
        "generation": generation,
        "logical_clock": 1000 + generation,
        "chunk_size": chunk_size,
        "compression": "delta_zlib",
        "delta_step": 2,
        "vocabulary": VOCABULARY,
        "resource_limits": {
            "degrade_policy": "error",
            "max_batch_docs": 100,
            "max_comparisons": 100000,
            "max_postings_bytes": 1048576,
        },
    }


def generate_samples(
    output_dir: str | Path,
    *,
    seed: int = 20261127,
    reset: bool = True,
) -> dict[str, Any]:
    output = Path(output_dir)
    if reset and output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    batch_rows: list[list[dict[str, Any]]] = []
    for batch_no in (1, 2, 3):
        rows = []
        for offset in range(1, 5):
            doc_id = batch_no * 1000 + offset
            rows.append(_document(rng, batch_no=batch_no, doc_id=doc_id))
        rows.sort(key=lambda row: row["doc_id"])
        batch_rows.append(rows)

    batch_dir = output / "batches"
    batch_hashes: dict[str, str] = {}
    batch_paths: list[str] = []
    for batch_no, rows in zip((1, 2, 3), batch_rows):
        name = f"batch_{batch_no:04d}.jsonl"
        path = batch_dir / name
        batch_hashes[name] = _write_jsonl(path, rows)
        batch_paths.append(str(path.relative_to(output)))

    replacement_rows = [dict(row) for row in batch_rows[1]]
    replacement_rows[0]["terms"] = [
        "recommend",
        "search",
        "rerank",
        "ranking",
        "query",
        "index",
        "search",
        "cache",
    ]
    replacement_rows[0]["doc_length"] = len(replacement_rows[0]["terms"])
    replacement_frequencies = Counter(replacement_rows[0]["terms"])
    replacement_positions: dict[str, list[int]] = {}
    for position, term in enumerate(replacement_rows[0]["terms"]):
        replacement_positions.setdefault(term, []).append(position)
    replacement_rows[0]["term_frequencies"] = dict(sorted(replacement_frequencies.items()))
    replacement_rows[0]["positions"] = dict(sorted(replacement_positions.items()))
    replacement_body = {
        key: replacement_rows[0][key]
        for key in (
            "batch_no",
            "doc_id",
            "doc_length",
            "term_frequencies",
            "positions",
            "terms",
        )
    }
    replacement_rows[0]["content_hash"] = sha256_json(replacement_body)
    replacement_rows[-1] = _document(
        rng,
        batch_no=2,
        doc_id=2005,
        forced_terms=["ledger", "recover", "checkpoint", "ledger", "snapshot"],
    )
    replacement_rows.sort(key=lambda row: row["doc_id"])
    replacement_name = "batch_0002_replacement.jsonl"
    replacement_hash = _write_jsonl(
        output / "batches_replace" / replacement_name, replacement_rows
    )

    config_dir = output / "config"
    v1_path = config_dir / "v1" / "index_config.json"
    _write_json(v1_path, _base_config(generation=1))
    v1_gen2_path = config_dir / "v1_generation2" / "index_config.json"
    _write_json(v1_gen2_path, _base_config(generation=2, chunk_size=5))
    upgraded = upgrade_v1_to_v2(
        v1_path, config_dir / "v2", shard_count=3, write_report=False
    )
    upgraded_gen2 = upgrade_v1_to_v2(
        v1_gen2_path,
        config_dir / "v2_generation2",
        shard_count=2,
        write_report=False,
    )

    invalid_dir = output / "invalid"
    missing = _base_config(generation=1)
    missing.pop("chunk_size")
    _write_json(invalid_dir / "v1_missing_chunk.json", missing)
    invalid_mode = _base_config(generation=1)
    invalid_mode["compression"] = "brotli-ish"
    _write_json(invalid_dir / "v1_bad_compression.json", invalid_mode)
    unsupported = _base_config(generation=1)
    unsupported["version"] = 9
    _write_json(invalid_dir / "unsupported_version.json", unsupported)

    fault_dir = output / "faults"
    _write_json(
        fault_dir / "fail_before_compress_checkpoint.json",
        {"phase": "compress", "when": "before_checkpoint"},
    )
    _write_json(
        fault_dir / "fail_after_postings_checkpoint.json",
        {"phase": "postings", "when": "after_checkpoint"},
    )
    _write_json(
        output / "recovery" / "resume.json",
        {"action": "resume", "clear_fault": True},
    )
    _write_json(
        output / "recovery" / "resume_from_v1.json",
        {"action": "resume", "from_checkpoint": 1},
    )

    output_resolved = output.resolve()
    manifest = {
        "batch_hashes": batch_hashes,
        "batches": batch_paths,
        "config_v1": str(v1_path.relative_to(output)),
        "config_v1_generation2": str(v1_gen2_path.relative_to(output)),
        "config_v2": str(Path(upgraded.target_path).relative_to(output_resolved)),
        "config_v2_generation2": str(
            Path(upgraded_gen2.target_path).relative_to(output_resolved)
        ),
        "replacement": str(
            (output / "batches_replace" / replacement_name).relative_to(output)
        ),
        "replacement_hash": replacement_hash,
        "seed": seed,
    }
    manifest["digest"] = sha256_json(manifest)
    _write_json(output / "samples_manifest.json", manifest)
    return manifest
