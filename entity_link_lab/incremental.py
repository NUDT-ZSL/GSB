"""Incremental sentence/revision handling and dependency invalidation."""

from __future__ import annotations

import copy
from typing import Any

from .cache import CacheStore, closure_cache_key, dependency_hash
from .canonical import content_hash
from .chains import ChainRegistry, build_chains, build_trace_records
from .evidence import compute_evidence, document_stats
from .pipeline import _within_window
from .rules import internal_hash


def apply_delta_to_rows(
    rows: list[dict[str, Any]], delta: dict[str, Any]
) -> list[dict[str, Any]]:
    updated = copy.deepcopy(rows)
    index = {(row["doc_id"], row["sent_id"]): i for i, row in enumerate(updated)}

    for row in delta.get("add_sentences", []):
        key = (row["doc_id"], row["sent_id"])
        if key in index:
            raise ValueError(f"cannot add existing sentence {key}")
        updated.append(copy.deepcopy(row))

    for row in delta.get("revise_sentences", []):
        key = (row["doc_id"], row["sent_id"])
        if key not in index:
            raise ValueError(f"cannot revise missing sentence {key}")
        updated[index[key]] = copy.deepcopy(row)

    updated.sort(key=lambda row: (row["doc_id"], row["sent_id"]))
    return updated


def delta_changed_mention_ids(delta: dict[str, Any]) -> list[str]:
    ids: set[str] = set()
    for key in ("add_sentences", "revise_sentences"):
        for row in delta.get(key, []):
            for mention in row.get("mentions", []):
                if mention.get("mention_id"):
                    ids.add(mention["mention_id"])
    return sorted(ids)


def rule_jump_basis(
    before_book: dict[str, Any], after_book: dict[str, Any] | None
) -> dict[str, Any]:
    before_hash = internal_hash(before_book)
    if after_book is None:
        return {
            "jumped": False,
            "rule_hash_before": before_hash,
            "rule_hash_after": before_hash,
            "generation_before": max(rule["generation"] for rule in before_book["rules"]),
            "generation_after": max(rule["generation"] for rule in before_book["rules"]),
            "changed_rule_ids": [],
            "basis": before_book["generation_timeline_ms"],
            "tombstoned_rule_ids": [
                rule["rule_id"] for rule in before_book["rules"] if rule["tombstone"]
            ],
        }

    before_rules = {rule["rule_id"]: rule for rule in before_book["rules"]}
    after_rules = {rule["rule_id"]: rule for rule in after_book["rules"]}
    changed: list[str] = []
    for rule_id in sorted(set(before_rules) | set(after_rules)):
        if rule_id not in before_rules or rule_id not in after_rules:
            changed.append(rule_id)
        elif content_hash(before_rules[rule_id]) != content_hash(after_rules[rule_id]):
            changed.append(rule_id)
    return {
        "jumped": before_hash != internal_hash(after_book),
        "rule_hash_before": before_hash,
        "rule_hash_after": internal_hash(after_book),
        "generation_before": max(rule["generation"] for rule in before_book["rules"]),
        "generation_after": max(rule["generation"] for rule in after_book["rules"]),
        "changed_rule_ids": changed,
        "basis": after_book["generation_timeline_ms"],
        "tombstoned_rule_ids": [
            rule["rule_id"] for rule in after_book["rules"] if rule["tombstone"]
        ],
    }


def run_incremental(
    *,
    baseline_report: dict[str, Any],
    updated_mentions: list[dict[str, Any]],
    delta: dict[str, Any],
    book: dict[str, Any],
    cache: CacheStore,
    registry: ChainRegistry | None = None,
    rule_book_after: dict[str, Any] | None = None,
) -> dict[str, Any]:
    after_book = rule_book_after or book
    changed_ids = set(delta_changed_mention_ids(delta))
    updated_by_id = {mention["mention_id"]: mention for mention in updated_mentions}
    missing = sorted(changed_ids - set(updated_by_id))
    if missing:
        raise ValueError(f"delta mentions missing after revision: {missing}")

    stats = document_stats(updated_mentions)
    baseline_evidence = {
        item["pair_key"]: item for item in baseline_report.get("exact_evidence", [])
    }
    jump = rule_jump_basis(book, rule_book_after)
    changed_rule_ids = set(jump["changed_rule_ids"])
    changed_docs = {
        updated_by_id[item]["doc_id"]
        for item in changed_ids
        if item in updated_by_id
    }

    recomputed_pair_keys: set[str] = set()
    affected_mention_ids: set[str] = {
        item["mention_id"]
        for item in updated_mentions
        if item["doc_id"] in changed_docs
    }
    ordered = sorted(updated_mentions, key=lambda item: item["order_index"])
    for index, mention in enumerate(ordered):
        for other in ordered[index + 1 :]:
            key = f"{mention['mention_id']}|{other['mention_id']}"
            data_touched = (
                mention["doc_id"] in changed_docs
                or other["doc_id"] in changed_docs
            )
            rule_touched = (
                key in baseline_evidence
                and baseline_evidence[key].get("rule_id") in changed_rule_ids
            )
            if not (data_touched or rule_touched):
                continue
            if not _within_window(mention, other, after_book):
                continue
            recomputed_pair_keys.add(key)
            affected_mention_ids.update(
                (mention["mention_id"], other["mention_id"])
            )

    final_evidence: list[dict[str, Any]] = []
    reused_evidence_keys: list[str] = []
    recomputed_evidence_keys: list[str] = []
    for key in sorted(set(baseline_evidence) | recomputed_pair_keys):
        if key in recomputed_pair_keys:
            left_id, right_id = key.split("|", 1)
            left = updated_by_id[left_id]
            right = updated_by_id[right_id]
            origins = baseline_evidence.get(key, {}).get(
                "candidate_origins", ["incremental_recompute"]
            )
            evidence = compute_evidence(left, right, after_book, stats, path="exact")
            evidence["candidate_origins"] = origins
            evidence["candidate_sources"] = origins
            evidence["invalidation_reason"] = (
                "data_dependency" if key in recomputed_pair_keys else "rule_dependency"
            )
            cache.put_current(evidence["cache_key"], internal_hash(after_book), evidence)
            final_evidence.append(evidence)
            recomputed_evidence_keys.append(key)
            continue

        old = baseline_evidence[key]
        cached = cache.get_current(old["cache_key"], internal_hash(book))
        if cached is not None:
            reused_evidence_keys.append(key)
            final_evidence.append(cached)
        else:
            reused_evidence_keys.append(key + ":baseline_report")
            final_evidence.append(old)

    bundle = build_chains(final_evidence, updated_mentions)
    traces = build_trace_records(bundle["chains"], {item["pair_key"]: item for item in final_evidence})
    current_dependency = dependency_hash(updated_mentions, after_book)
    for trace in traces:
        trace["dependency_hash"] = current_dependency
        trace["cache_served"] = False

    baseline_chains = {chain["chain_id"]: chain for chain in baseline_report.get("chains", [])}
    reused_closures: list[dict[str, Any]] = []
    recomputed_closures: list[dict[str, Any]] = []
    if registry is not None:
        for chain in baseline_report.get("chains", []):
            if chain["chain_id"] not in registry.ids():
                registry.register(chain)

    for chain in bundle["chains"]:
        old_chain = baseline_chains.get(chain["chain_id"])
        cache_key = closure_cache_key(chain, internal_hash(after_book))
        unchanged = (
            old_chain is not None
            and old_chain.get("chain_checksum") == chain.get("chain_checksum")
        )
        if unchanged:
            cached = cache.get_current(cache_key, internal_hash(after_book))
            reused_closures.append(
                {
                    "chain_id": chain["chain_id"],
                    "cache_key": cache_key,
                    "cache_hit": cached is not None,
                    "structure_unchanged": True,
                    "member_ids": chain["member_ids"],
                }
            )
        else:
            cache.put_current(
                cache_key,
                internal_hash(after_book),
                {"chain": chain, "recomputed_pair_keys": recomputed_evidence_keys},
            )
            recomputed_closures.append(
                {
                    "chain_id": chain["chain_id"],
                    "cache_key": cache_key,
                    "structure_changed": old_chain is not None,
                    "member_ids": chain["member_ids"],
                }
            )
            if registry is not None:
                if chain["chain_id"] in registry.ids():
                    registry.replace_after_invalidation(chain)
                else:
                    registry.register(chain)

    affected_chain_ids = sorted(
        item["chain_id"]
        for item in recomputed_closures
    )
    unaffected_reused_chain_ids = sorted(item["chain_id"] for item in reused_closures)
    covered = set(affected_mention_ids)
    result = {
        "status": "ok",
        "delta_id": delta.get("delta_id"),
        "changed_mention_ids": sorted(changed_ids),
        "affected_mention_ids": sorted(affected_mention_ids),
        "covered_affected_mention_ids": sorted(covered),
        "recomputed_pair_keys": sorted(recomputed_pair_keys),
        "recomputed_evidence_keys": sorted(recomputed_evidence_keys),
        "reused_evidence_keys": sorted(reused_evidence_keys),
        "affected_chain_ids": affected_chain_ids,
        "recomputed_closures": recomputed_closures,
        "reused_closures": reused_closures,
        "unaffected_reused_chain_ids": unaffected_reused_chain_ids,
        "rule_generation_jump": jump,
        "dependency_hash": current_dependency,
        "chains": bundle["chains"],
        "conflict_decisions": bundle["conflict_decisions"],
        "traces": traces,
        "exact_evidence": final_evidence,
        "counts": {
            "mentions": len(updated_mentions),
            "chains": len(bundle["chains"]),
            "recomputed_pairs": len(recomputed_pair_keys),
            "reused_evidence": len(reused_evidence_keys),
            "reused_closures": len(reused_closures),
            "recomputed_closures": len(recomputed_closures),
        },
    }
    return result
