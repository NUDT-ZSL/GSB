from __future__ import annotations

import json

import pytest

from entity_link_lab.canonical import canonical_bytes
from entity_link_lab.chains import ChainRegistry
from entity_link_lab.errors import E_INCONSISTENT, E_STALE_LINK, LinkError
from entity_link_lab.evidence import candidate_sort_key
from entity_link_lab.ingest import load_mentions
from entity_link_lab.replay import replay_traces
from entity_link_lab.rules import read_rule_book
from entity_link_lab.selfcheck import run_self_checks


def test_pipeline_counts_and_self_check(report):
    assert report["status"] == "ok"
    assert report["counts"]["mentions"] == 21
    assert report["counts"]["exact_candidates"] == 21
    assert report["counts"]["approx_candidates"] == 21
    assert report["self_check"]["ok"] is True
    assert report["replay"]["ok"] is True
    assert report["counts"]["chains"] >= 6
    assert report["counts"]["conflicts"] >= 1


def test_candidate_files_are_byte_deterministic(sample_dir, tmp_path):
    from entity_link_lab.pipeline import run_pipeline

    first = tmp_path / "a"
    second = tmp_path / "b"
    run_pipeline(
        sample_dir,
        output_dir=first / "out",
        cache_dir=first / "cache",
        checkpoint_dir=first / "ckpt",
    )
    run_pipeline(
        sample_dir,
        output_dir=second / "out",
        cache_dir=second / "cache",
        checkpoint_dir=second / "ckpt",
    )
    left = (first / "out" / "candidates_exact.jsonl").read_bytes()
    right = (second / "out" / "candidates_exact.jsonl").read_bytes()
    assert left == right
    report_left = json.loads((first / "out" / "report.json").read_text())
    report_right = json.loads((second / "out" / "report.json").read_text())
    assert canonical_bytes(report_left) == canonical_bytes(report_right)


def test_score_components_recompute_to_total(report):
    for evidence in report["exact_evidence"]:
        total = sum(item["contribution_bps"] for item in evidence["score"]["components"])
        assert total == evidence["score"]["total_bps"]
        assert evidence["score"]["recomputed_total_bps"] == total


def test_approx_records_prune_low_discrimination_features(report):
    for evidence in report["approx_evidence"]:
        names = {item["name"] for item in evidence["pruned_features"]}
        assert "mention_order_parity" in names
        assert "sentence_id_parity" in names
    plan = report["approx_plan"]
    assert plan["bucket_count"] >= 4
    assert all("filter_basis" in bucket for bucket in plan["buckets"])


def test_tie_break_key_uses_codepoint_then_type():
    base = {
        "score": {"total_bps": 7000},
        "rule_generation": 2,
        "pair_key": "k",
        "first_mention": {"start": 3, "entity_type": "PER"},
    }
    earlier = candidate_sort_key(base)
    later_record = json.loads(json.dumps(base))
    later_record["first_mention"]["start"] = 9
    later = candidate_sort_key(later_record)
    assert earlier < later


def test_trace_replay_and_stale_link(sample_dir, report):
    mentions = load_mentions(sample_dir / "mentions.jsonl")
    book = read_rule_book(sample_dir / "rules_v2" / "manifest.json")
    result = replay_traces(report["traces"], mentions, book)
    assert result["ok"] is True

    stale_trace = dict(report["traces"][0])
    stale_trace["cache_key_version"] = "ckv0-old"
    stale_trace["cache_served"] = True
    with pytest.raises(LinkError) as exc:
        replay_traces([stale_trace], mentions, book)
    assert exc.value.code == E_STALE_LINK


def test_chain_registry_rejects_duplicates(report):
    registry = ChainRegistry()
    chain = report["chains"][0]
    registry.register(chain)
    with pytest.raises(LinkError) as exc:
        registry.register(chain)
    assert exc.value.code == "E_DUPLICATE_CHAIN"


def test_self_check_detects_path_inconsistency(report):
    mutated = json.loads(json.dumps(report["approx_evidence"]))
    mutated[0]["score"]["total_bps"] += 1
    with pytest.raises(LinkError) as exc:
        run_self_checks(
            exact_evidence=report["exact_evidence"],
            approx_evidence=mutated,
            exact_chains=report["chains"],
            approx_chains=report["chains"],
            traces=report["traces"],
            evidence_by_key={
                item["pair_key"]: item for item in report["exact_evidence"]
            },
        )
    assert exc.value.code == E_INCONSISTENT
