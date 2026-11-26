from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, atomic_write_json, digest_file
from .config import (
    default_config_payload,
    migrate_v1_to_v2,
    write_v1,
)


SEED = 20261127
VOCAB = (
    "alpha",
    "alpine",
    "alto",
    "amber",
    "beta",
    "beacon",
    "berry",
    "blue",
    "cobalt",
    "copper",
    "coral",
    "crimson",
    "delta",
    "dune",
    "dust",
    "echo",
    "ember",
    "epsilon",
    "fern",
    "frost",
    "gamma",
    "glow",
    "granite",
    "green",
    "harbor",
    "haze",
    "indigo",
    "iota",
    "ivory",
    "jade",
    "kappa",
    "kelp",
    "lambda",
    "lumen",
    "maple",
    "mist",
    "mu",
    "nu",
    "omega",
    "omicron",
    "opal",
    "pearl",
    "quartz",
    "rho",
    "river",
    "sigma",
    "slate",
    "stone",
    "tau",
    "theta",
    "tide",
    "upsilon",
    "violet",
    "willow",
    "xi",
    "zephyr",
    "zulu",
)
CATEGORIES = ("books", "electronics", "home", "sports")
QUERY_TERMS = (
    ("alpha", "beta"),
    ("gamma", "delta"),
    ("epsilon", "zeta"),
    ("theta", "iota"),
    ("kappa", "lambda"),
    ("alpha", "gamma"),
    ("mu", "nu"),
    ("xi", "omicron"),
    ("rho", "sigma"),
    ("tau", "upsilon"),
    ("zulu",),
    ("omega",),
)


def _doc_jsonl(documents: list[dict[str, Any]]) -> bytes:
    lines = []
    for doc in sorted(documents, key=lambda item: item["doc_id"]):
        lines.append(
            json.dumps(
                doc,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _query_jsonl(queries: list[dict[str, Any]]) -> bytes:
    lines = []
    for query in sorted(queries, key=lambda item: item["query_id"]):
        lines.append(
            json.dumps(
                query,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_sample_data(seed: int = SEED) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rng = random.Random(seed)
    common_terms = [term for term in VOCAB if term not in {"zulu", "omega"}]
    category_pools = {
        "books": ["alpha", "amber", "echo", "ivory", "maple", "pearl"],
        "electronics": ["cobalt", "copper", "echo", "frost", "lumen", "quartz"],
        "home": ["coral", "ember", "fern", "haze", "stone", "willow"],
        "sports": ["blue", "dune", "green", "river", "slate", "tide"],
    }
    documents: list[dict[str, Any]] = []
    for index in range(32):
        category = CATEGORIES[index % len(CATEGORIES)]
        term_count = 4 + (index % 4)
        selected = set(category_pools[category][: 2 + index % 4])
        while len(selected) < term_count:
            selected.add(rng.choice(common_terms))
        terms = []
        for term in sorted(selected):
            tf = 1 + (rng.randrange(3) if rng.random() < 0.55 else 0)
            terms.append({"term": term, "tf": tf})
        doc_len = sum(item["tf"] for item in terms) + 2 + index % 5
        documents.append(
            {
                "doc_id": f"d{index + 1:04d}",
                "terms": terms,
                "doc_len": doc_len,
                "category": category,
                "quality_score": round(0.35 + (index * 37 % 60) / 100.0, 2),
            }
        )
    target = documents[6]
    if not any(item["term"] == "alpha" for item in target["terms"]):
        target["terms"].append({"term": "alpha", "tf": 2})
        target["terms"].sort(key=lambda item: item["term"])
        target["doc_len"] += 2
    queries: list[dict[str, Any]] = []
    for index, terms in enumerate(QUERY_TERMS):
        query: dict[str, Any] = {
            "query_id": f"q{index + 1:04d}",
            "terms": list(terms),
            "top_k": 5,
        }
        if index in (2, 7):
            query["filters"] = {"category": CATEGORIES[index % len(CATEGORIES)]}
        elif index in (4, 8):
            query["filters"] = {"category": CATEGORIES[index % len(CATEGORIES)], "min_quality": 0.5}
        queries.append(query)
    return documents, queries


def build_tf_revision(
    documents: list[dict[str, Any]], target_doc: str = "d0007"
) -> list[dict[str, Any]]:
    revised = copy.deepcopy(documents)
    for doc in revised:
        if doc["doc_id"] != target_doc:
            continue
        changed = False
        for item in doc["terms"]:
            if item["term"] == "alpha":
                item["tf"] += 2
                changed = True
                break
        if not changed:
            raise RuntimeError("revision target does not contain alpha")
        if any(item["tf"] > doc["doc_len"] for item in doc["terms"]):
            raise RuntimeError("revision made tf exceed doc_len")
        return revised
    raise RuntimeError(f"revision target not found: {target_doc}")


def generate_workspace(destination: str | Path, *, seed: int = SEED) -> dict[str, Any]:
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    variants = root / "variants"
    variants.mkdir(parents=True, exist_ok=True)
    documents, queries = build_sample_data(seed)
    atomic_write_bytes(
        root / "vocab.txt", ("\n".join(sorted(VOCAB)) + "\n").encode("utf-8")
    )
    atomic_write_bytes(root / "docs.jsonl", _doc_jsonl(documents))
    atomic_write_bytes(root / "queries.jsonl", _query_jsonl(queries))
    revised = build_tf_revision(documents)
    atomic_write_bytes(
        variants / "docs_tf_revision.jsonl", _doc_jsonl(revised)
    )
    base_payload = default_config_payload(generation=1)
    base_v1_path = root / "config_v1.json"
    write_v1(
        base_v1_path,
        base_payload,
        config_id="fusion-main-v1",
        effective_seconds=0.0,
    )
    v2_dir = root / "config_v2"
    migration = migrate_v1_to_v2(base_v1_path, v2_dir)
    weight_payload = default_config_payload(generation=2, prefix_weight=0.3)
    weight_payload["recalls"]["lexical"]["weight"] = 0.25
    weight_v1 = variants / "config_prefix_weight_v1.json"
    write_v1(
        weight_v1,
        weight_payload,
        config_id="fusion-prefix-weight-v2",
        effective_seconds=60.0,
    )
    normalization_payload = default_config_payload(
        generation=3, normalization="reciprocal_rank"
    )
    normalization_v1 = variants / "config_rank_norm_v1.json"
    write_v1(
        normalization_v1,
        normalization_payload,
        config_id="fusion-rank-norm-v3",
        effective_seconds=120.0,
    )
    manifest = {
        "schema": "fusion_diag.samples.v1",
        "seed": seed,
        "documents": len(documents),
        "queries": len(queries),
        "revision_doc": "d0007",
        "revision_term": "alpha",
        "files": {},
        "migration": migration,
    }
    for relative in (
        "vocab.txt",
        "docs.jsonl",
        "queries.jsonl",
        "config_v1.json",
        "config_v2/manifest.json",
        "config_v2/metadata.sqlite",
        "config_v2/shards/recall_lexical.json",
        "config_v2/shards/recall_prefix.json",
        "config_v2/shards/recall_category.json",
        "config_v2/shards/recall_vector.json",
        "config_v2/shards/fusion.json",
        "variants/docs_tf_revision.jsonl",
        "variants/config_prefix_weight_v1.json",
        "variants/config_rank_norm_v1.json",
    ):
        path = root / relative
        manifest["files"][relative] = {
            "sha256": digest_file(path),
            "bytes": path.stat().st_size,
        }
    atomic_write_json(root / "sample_manifest.json", manifest)
    return manifest
