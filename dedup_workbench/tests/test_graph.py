from __future__ import annotations

from pathlib import Path

from dedup_workbench.canonical import canonical_bytes
from dedup_workbench.compare import compare_pair
from dedup_workbench.engine import DedupWorkbench
from dedup_workbench.signatures import build_signature
from dedup_workbench.testing import base_config, make_record, write_jsonl


def run_pipeline(tmp_path: Path, records, config=None, *, input_name="corpus.jsonl", runtime_name="runtime"):
    input_path = tmp_path / input_name
    write_jsonl(input_path, records)
    workbench = DedupWorkbench(tmp_path / runtime_name, config or base_config())
    return workbench.run(input_path)


def test_candidate_window_never_compares_outside_rank_distance(tmp_path):
    text = "same fixed blocks alpha beta gamma delta"
    records = [make_record(f"doc-{index}", text, quality_score=0.5) for index in range(6)]
    config = base_config(candidate_window=2)
    report = run_pipeline(tmp_path, records, config)

    for pair in report["candidates"]["pairs"]:
        assert pair["source_buckets"]
        for source in pair["source_buckets"]:
            assert source["rank_distance"] <= 2

    pair_ids = {pair["pair_id"] for pair in report["candidates"]["pairs"]}
    assert "doc-0⇔doc-3" not in pair_ids
    assert "doc-0⇔doc-2" in pair_ids
    assert report["self_check"]["ok"] is True


def test_document_anchor_digest_buckets_are_document_level_summary(tmp_path):
    text = "anchor bucket deterministic text with several blocks for hashing"
    records = [
        make_record("anchor-a", text, quality_score=0.9),
        make_record("anchor-b", text, quality_score=0.5),
        make_record("anchor-c", "unrelated text with different block digests"),
    ]
    config = base_config(bucket_strategy="anchor_digest", bucket_anchor_count=2)
    report = run_pipeline(tmp_path, records, config)
    signature_a = next(item for item in report["signatures"] if item["doc_id"] == "anchor-a")
    assert len(signature_a["document_bucket_digests"]) == 2
    assert all(key.startswith("doc_anchor:") for key in signature_a["bucket_keys"])
    pair_ids = {pair["pair_id"] for pair in report["candidates"]["pairs"]}
    assert "anchor-a⇔anchor-b" in pair_ids
    assert not any("anchor-c" in pair_id and ("anchor-a" in pair_id or "anchor-b" in pair_id) for pair_id in pair_ids)


def test_comparison_records_offsets_and_digest_conflicts():
    config = base_config(chunk_signature_length=4)
    doc_a = make_record("a", "aaaabbbbccccdddd")
    doc_b = make_record("b", "aaaaXXXXccccYYYY")
    sig_a = build_signature(doc_a, config)
    sig_b = build_signature(doc_b, config)
    pair = {
        "pair_id": "a⇔b",
        "doc_id_a": "a",
        "doc_id_b": "b",
        "source_buckets": [{"bucket_key": "block:test"}],
        "primary_bucket": "block:test",
    }
    result = compare_pair(pair, {"a": sig_a, "b": sig_b}, config)
    assert result["hit_count"] == 2
    assert result["offset_counts"] == {"0": 2}
    assert result["aligned_conflict_count"] == 2
    assert result["digest_conflict_status"] == "conflict"
    assert [hit["position_a"] for hit in result["hit_positions"]] == [0, 2]
    assert len(result["aligned_checks"]) == 4

    digest_config = base_config(chunk_signature_length=4, validation_mode="digest")
    digest_result = compare_pair(pair, {"a": sig_a, "b": sig_b}, digest_config)
    assert "aligned_checks" not in digest_result
    assert digest_result["digest_probe"]["aligned_conflict_count"] == 2
    assert digest_result["hit_positions"] == result["hit_positions"]


def test_representative_uses_quality_then_doc_id(tmp_path):
    text = "identical representative selection text with several signature blocks"
    records = [
        make_record("doc-low", text, quality_score=0.20),
        make_record("doc-high", text, quality_score=0.95),
        make_record("doc-mid", text, quality_score=0.60),
    ]
    report = run_pipeline(tmp_path, records, base_config())
    decision = report["decisions"]["decisions"]["doc-high"]
    assert decision["status"] == "representative"
    assert report["decisions"]["removed_doc_ids"] == ["doc-low", "doc-mid"]
    removed = report["decisions"]["decisions"]["doc-low"]
    assert removed["representative_doc_id"] == "doc-high"
    assert removed["near_duplicate_neighbors"]
    neighbor = removed["near_duplicate_neighbors"][0]
    assert neighbor["hit_positions"]
    assert neighbor["threshold_check"]["passed"] is True
    assert "similarity_exact" in neighbor["threshold_check"]


def test_tied_quality_uses_doc_id_ascending(tmp_path):
    text = "tied quality representative text repeated enough for blocks"
    records = [
        make_record("doc-zeta", text, quality_score=0.8),
        make_record("doc-alpha", text, quality_score=0.8),
        make_record("doc-middle", text, quality_score=0.8),
    ]
    report = run_pipeline(tmp_path, records, base_config())
    assert report["decisions"]["removed_doc_ids"] == ["doc-middle", "doc-zeta"]
    assert report["decisions"]["decisions"]["doc-alpha"]["status"] == "representative"


def test_same_inputs_and_config_are_byte_identical(tmp_path):
    records = [
        make_record("doc-1", "byte identical deterministic text alpha"),
        make_record("doc-2", "byte identical deterministic text alpha"),
        make_record("doc-3", "different deterministic text beta"),
    ]
    first = run_pipeline(tmp_path, records, base_config(), input_name="a.jsonl", runtime_name="rt-a")
    second = run_pipeline(tmp_path, records, base_config(), input_name="b.jsonl", runtime_name="rt-b")

    for key in ("signatures", "candidates", "comparisons", "decisions", "doc_records"):
        assert canonical_bytes(first[key]) == canonical_bytes(second[key]), key

    first_export = (tmp_path / "rt-a" / "exports" / "doc_records.jsonl").read_bytes()
    second_export = (tmp_path / "rt-b" / "exports" / "doc_records.jsonl").read_bytes()
    assert first_export == second_export
