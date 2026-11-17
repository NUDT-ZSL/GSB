from __future__ import annotations

from intent_engine.loader import load_corpus
from intent_engine.resources import load_resources


def test_loader_reports_codes_and_continues(data_dir, resources_dir, config_path):
    resources = load_resources(resources_dir, config_path)
    result = load_corpus(
        data_dir / "invalid_corpus.jsonl",
        intent_names=resources.intent_names,
        allowed_types=resources.entity_types,
    )
    codes = {failure["business_code"] for failure in result.failures}
    assert {"E_EMPTY_SENTENCE", "E_BAD_RECORD", "E_UNKNOWN_INTENT", "E_SPAN_OUT_OF_RANGE"} <= codes
    assert [sample.sample_id for sample in result.samples] == ["BAD-OK", "C001"]
    assert result.source_lines == 7
    assert result.failure_count == 5


def test_loader_valid_corpus_has_unique_sorted_ids(corpus_path, resources_dir, config_path):
    resources = load_resources(resources_dir, config_path)
    result = load_corpus(
        corpus_path,
        intent_names=resources.intent_names,
        allowed_types=resources.entity_types,
    )
    ids = [sample.sample_id for sample in result.samples]
    assert result.failures == []
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids)) == 15

