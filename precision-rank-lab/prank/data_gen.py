"""Deterministic local sample generation.

All randomness comes from a seeded :class:`numpy.random.Generator`; iteration
order only depends on sorted ids, so the JSONL output is byte-identical across
runs and machines for the same seed and size parameters.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .errors import BusinessError
from .models import CandidateSet, Item, Query

VOCAB = tuple(f"t{i:03d}" for i in range(120))
CATEGORIES = ("electronics", "books", "home", "fashion", "grocery", "sports")


def generate_dataset(seed: int, n_queries: int, min_items: int, max_items: int,
                     n_terms_per_query: int = 5, n_terms_per_item: int = 8):
    rng = np.random.default_rng(seed)
    candidates: list[CandidateSet] = []
    for qi in range(n_queries):
        query_id = f"q{qi:04d}"
        q_terms = tuple(sorted(rng.choice(VOCAB, size=n_terms_per_query, replace=False)))
        query = Query(query_id=query_id,
                      query_text=" ".join(q_terms),
                      query_terms=q_terms)
        n_items = int(rng.integers(min_items, max_items + 1))
        items: list[Item] = []
        for ii in range(n_items):
            item_id = f"{query_id}-i{ii:03d}"
            i_terms = tuple(sorted(rng.choice(VOCAB, size=n_terms_per_item, replace=False)))
            category = str(rng.choice(CATEGORIES))
            # round to fixed decimals so JSON output is canonical
            price = round(float(rng.uniform(1.0, 500.0)), 4)
            quality = round(float(rng.uniform(0.1, 1.0)), 4)
            items.append(Item(item_id=item_id, item_terms=i_terms,
                              category=category, price=price, quality=quality))
        items.sort(key=lambda it: it.item_id)
        candidates.append(CandidateSet(query=query, items=items))
    return candidates


def write_jsonl(candidates, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    queries_path = out / "queries.jsonl"
    items_path = out / "items.jsonl"
    with queries_path.open("w", encoding="utf-8", newline="\n") as qf, \
            items_path.open("w", encoding="utf-8", newline="\n") as inf:
        for cand in sorted(candidates, key=lambda c: c.query.query_id):
            qf.write(json.dumps({
                "query_id": cand.query.query_id,
                "query_text": cand.query.query_text,
                "query_terms": list(cand.query.query_terms),
            }, sort_keys=True) + "\n")
            for item in cand.items:
                inf.write(json.dumps({
                    "query_id": cand.query.query_id,
                    "item_id": item.item_id,
                    "item_terms": list(item.item_terms),
                    "category": item.category,
                    "price": item.price,
                    "quality": item.quality,
                }, sort_keys=True) + "\n")
    return queries_path, items_path


def read_jsonl(in_dir: str | Path):
    base = Path(in_dir)
    queries: dict[str, Query] = {}
    items_by_query: dict[str, list[Item]] = {}
    with (base / "queries.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            qid = row["query_id"]
            if qid in queries:
                raise BusinessError(
                    "E_DUP_QUERY", "data_load",
                    f"duplicate query_id {qid} in queries.jsonl",
                    query_id=qid,
                )
            queries[qid] = Query(qid, row["query_text"], tuple(row["query_terms"]))
            items_by_query[qid] = []
    with (base / "items.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            qid = row["query_id"]
            if qid not in items_by_query:
                raise BusinessError(
                    "E_INPUT_INVALID", "data_load",
                    f"item references unknown query_id {qid}", query_id=qid,
                )
            items_by_query[qid].append(Item(
                item_id=row["item_id"],
                item_terms=tuple(row["item_terms"]),
                category=row["category"],
                price=float(row["price"]),
                quality=float(row["quality"]),
            ))
    result = []
    for qid in sorted(queries):
        items = sorted(items_by_query[qid], key=lambda it: it.item_id)
        result.append(CandidateSet(query=queries[qid], items=items))
    return result


def apply_data_injections(candidates, injections: list[dict], bounds: dict):
    """Return a mutated copy of the dataset according to a fault-injection manifest."""
    by_id = {c.query.query_id: c for c in candidates}
    mutated = [CandidateSet(query=c.query, items=list(c.items)) for c in candidates]
    by_id_mut = {c.query.query_id: c for c in mutated}
    records: list[dict] = []
    for inj in injections:
        kind = inj["type"]
        target = inj.get("target_query")
        if kind == "dup_query":
            if target not in by_id_mut:
                raise BusinessError("E_INPUT_INVALID", "inject",
                                    f"injection target {target} not found", query_id=target)
            src = by_id_mut[target]
            dup = CandidateSet(query=src.query, items=list(src.items))
            mutated.append(dup)
            records.append({"type": kind, "query_id": target})
        elif kind == "empty_item_terms":
            cand = by_id_mut[target]
            offset = int(inj.get("target_item_offset", 0))
            old = cand.items[offset]
            cand.items[offset] = Item(old.item_id, (), old.category, old.price, old.quality)
            records.append({"type": kind, "query_id": target, "item_id": old.item_id})
        elif kind == "value_range":
            cand = by_id_mut[target]
            field_name = inj["field"]
            bad_value = float(inj["bad_value"])
            offset = int(inj.get("target_item_offset", 0))
            old = cand.items[offset]
            if field_name == "price":
                cand.items[offset] = Item(old.item_id, old.item_terms, old.category,
                                          bad_value, old.quality)
            elif field_name == "quality":
                cand.items[offset] = Item(old.item_id, old.item_terms, old.category,
                                          old.price, bad_value)
            else:
                raise BusinessError("E_INPUT_INVALID", "inject",
                                    f"unknown injection field {field_name}", query_id=target)
            records.append({"type": kind, "query_id": target, "field": field_name,
                            "bad_value": bad_value})
        else:
            raise BusinessError("E_INPUT_INVALID", "inject",
                                f"unknown data injection type {kind!r}")
    _ = bounds
    return mutated, records
