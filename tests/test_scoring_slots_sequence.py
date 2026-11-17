from __future__ import annotations

from intent_engine.intent import IntentScorer
from intent_engine.loader import load_corpus
from intent_engine.normalization import Normalizer
from intent_engine.resources import load_resources
from intent_engine.sequence import BIESOTagger, END_LABELS, START_LABELS, VALID_TRANSITIONS

from .conftest import execute


def test_scoring_is_deterministic_and_precision_limited(resources_dir, config_path, corpus_path):
    resources = load_resources(resources_dir, config_path)
    corpus = load_corpus(corpus_path, intent_names=resources.intent_names, allowed_types=resources.entity_types)
    normalizer = Normalizer(resources.config, resources.synonyms)
    scorer = IntentScorer(resources)
    sample = corpus.samples[0]
    normalized = normalizer.normalize(sample)
    first = scorer.score(normalized)
    second = scorer.score(normalized)
    assert first == second
    assert first["intent"] == sample.intent
    assert 0.0 <= first["confidence"] <= 1.0
    for value in first["scores"].values():
        assert round(value, resources.config.digits) == value
    for value in first["normalized_scores"].values():
        assert round(value, resources.config.digits) == value


def test_equal_scores_choose_lexicographic_intent_name():
    chosen, normalized = IntentScorer._normalize({"乙意图": 1.25, "甲意图": 1.25}, 6)
    assert chosen == "乙意图"
    assert normalized["乙意图"] == normalized["甲意图"]


def test_longest_match_suppression_downgrade_and_non_overlap(tmp_path):
    result = execute(state_dir=tmp_path / "state", workers=1)
    by_id = {item["sample_id"]: item for item in result.results}
    c003 = by_id["C003"]
    assert [(slot["type"], slot["text"]) for slot in c003["slots"]] == [("地点", "北京市"), ("地点", "朝阳区")]
    assert any(item["text"] == "北京" and item["reason"].startswith("overlap") for item in c003["suppressed_slots"])
    c013 = by_id["C013"]
    assert c013["downgraded_slots"][0]["reason"] == "intent_entity_incompatible"
    assert c013["downgraded_slots"][0]["weight"] == 0.45
    for item in result.results:
        slots = item.get("slots", [])
        for previous, current in zip(slots, slots[1:]):
            assert previous["end"] <= current["start"]


def test_bieso_path_is_legal_and_scores_are_complete(resources_dir, config_path, corpus_path):
    resources = load_resources(resources_dir, config_path)
    corpus = load_corpus(corpus_path, intent_names=resources.intent_names, allowed_types=resources.entity_types)
    normalizer = Normalizer(resources.config, resources.synonyms)
    tagger = BIESOTagger(resources)
    sample = next(item for item in corpus.samples if item.sample_id == "C006")
    normalized = normalizer.normalize(sample)
    tagged = tagger.tag(normalized)
    labels = tagged["labels"]
    assert len(labels) == len(normalized["chars"])
    assert labels[0] in START_LABELS
    assert labels[-1] in END_LABELS
    for previous, current in zip(labels, labels[1:]):
        assert current in VALID_TRANSITIONS[previous]
    assert len(tagged["position_scores"]) == len(labels)
    for row in tagged["position_scores"]:
        assert set(row["emission_scores"]) == {"B", "I", "E", "S", "O"}
    assert isinstance(tagged["path_score"], float)


def test_merge_records_covered_sequence_fragment(tmp_path):
    result = execute(state_dir=tmp_path / "state", workers=1)
    c006 = next(item for item in result.results if item["sample_id"] == "C006")
    assert c006["sequence"]["fragments"]
    assert c006["merge"]["covered_fragments"]
    assert all(item["reason"] == "same_span_lower_source_priority" for item in c006["merge"]["covered_fragments"])
