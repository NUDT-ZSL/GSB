from __future__ import annotations

import copy

from entity_link_lab.cache import CacheStore
from entity_link_lab.chains import ChainRegistry
from entity_link_lab.evidence import generate_approx_candidates
from entity_link_lab.chains import build_chains
from entity_link_lab.incremental import apply_delta_to_rows, run_incremental
from entity_link_lab.canonical import read_json, read_jsonl, write_jsonl
from entity_link_lab.ingest import load_mentions, load_seed_candidates
from entity_link_lab.rules import read_rule_book
from entity_link_lab.selfcheck import (
    check_invalidation_coverage,
    compare_evidence_paths,
)


def _incremental(sample_dir, run_dir, tmp_path):
    report = read_json(run_dir / "report.json")
    delta = read_json(sample_dir / "incremental_delta.json")
    rows = read_jsonl(sample_dir / "mentions.jsonl")
    updated_rows = apply_delta_to_rows(rows, delta)
    updated_jsonl = tmp_path / "updated.jsonl"
    write_jsonl(updated_jsonl, updated_rows)
    mentions = load_mentions(updated_jsonl)
    book = read_rule_book(sample_dir / "rules_v2" / "manifest.json")
    cache = CacheStore(run_dir / "cache")
    result = run_incremental(
        baseline_report=report,
        updated_mentions=mentions,
        delta=delta,
        book=book,
        cache=cache,
        registry=ChainRegistry(),
    )
    return result, mentions, book


def test_incremental_recomputes_only_changed_docs(sample_dir, run_dir, tmp_path):
    result, _, _ = _incremental(sample_dir, run_dir, tmp_path)
    assert "m-0022" in result["affected_mention_ids"]
    assert "m-0021" in result["affected_mention_ids"]
    assert "m-0020|m-0021" in result["recomputed_pair_keys"]
    assert any(key.startswith("m-0015|m-0022") for key in result["recomputed_pair_keys"])
    assert all(
        "m-0001" not in mention_id
        for mention_id in result["affected_mention_ids"]
    )


def test_unaffected_closures_reuse_cache(sample_dir, run_dir, tmp_path):
    result, _, _ = _incremental(sample_dir, run_dir, tmp_path)
    assert len(result["reused_closures"]) >= 4
    assert all(item["cache_hit"] for item in result["reused_closures"])
    assert all(item["structure_unchanged"] for item in result["reused_closures"])
    reused_docs = set()
    for item in result["reused_closures"]:
        if "m-0001" in item["member_ids"] or "m-0011" in item["member_ids"]:
            reused_docs.add("stable")
    assert reused_docs == {"stable"}


def test_incremental_self_check_and_coverage(sample_dir, run_dir, tmp_path):
    result, mentions, book = _incremental(sample_dir, run_dir, tmp_path)
    coverage = check_invalidation_coverage(result)
    assert coverage["ok"] is True

    seed_pairs = load_seed_candidates(sample_dir / "coref_candidates.jsonl", mentions)
    approx, _ = generate_approx_candidates(mentions, book, seed_pairs)
    approx_chains = build_chains(approx, mentions)["chains"]
    check = compare_evidence_paths(
        result["exact_evidence"], approx, result["chains"], approx_chains
    )
    assert check["ok"] is True


def test_rule_generation_jump_recorded(sample_dir, run_dir, tmp_path):
    result, _, _ = _incremental(sample_dir, run_dir, tmp_path)
    jump = result["rule_generation_jump"]
    assert jump["jumped"] is False
    assert jump["generation_before"] == 2
    assert jump["generation_after"] == 2
    assert any(item["generation"] == 1 for item in jump["basis"])


def test_algebra_jump_targets_rule_dependent_pairs(sample_dir, run_dir, tmp_path):
    report = read_json(run_dir / "report.json")
    delta = read_json(sample_dir / "incremental_delta.json")
    rows = read_jsonl(sample_dir / "mentions.jsonl")
    updated_rows = apply_delta_to_rows(rows, delta)
    updated_jsonl = tmp_path / "rule-jump.jsonl"
    write_jsonl(updated_jsonl, updated_rows)
    mentions = load_mentions(updated_jsonl)
    before = read_rule_book(sample_dir / "rules_v2" / "manifest.json")
    after = copy.deepcopy(before)
    for rule in after["rules"]:
        if rule["rule_id"] == "ctx-org":
            rule["generation"] = 3
            rule["algebra_generation"] = 3
            rule["algebra_bias_raw_bps"] = 3_600
            rule["effective_at_ms"] += 7_200_000
    after["generation_timeline_ms"].append(
        {"generation": 3, "effective_at_ms": before["generation_timeline_ms"][-1]["effective_at_ms"] + 7_200_000}
    )
    result = run_incremental(
        baseline_report=report,
        updated_mentions=mentions,
        delta=delta,
        book=before,
        rule_book_after=after,
        cache=CacheStore(run_dir / "rule-jump-cache"),
        registry=ChainRegistry(),
    )
    jump = result["rule_generation_jump"]
    assert jump["jumped"] is True
    assert jump["generation_after"] == 3
    assert "ctx-org" in jump["changed_rule_ids"]
    assert "m-0001|m-0004" in result["recomputed_pair_keys"]
