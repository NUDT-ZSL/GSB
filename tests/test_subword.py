import pytest

from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.subword import (SubwordConfig, best_candidate, segment_pairs,
                                 token_candidates)


def _records():
    return [
        {"pair_id": "p000", "src_tokens": ["abab", "cd"], "tgt_tokens": ["efef"]},
        {"pair_id": "p001", "src_tokens": ["abab"], "tgt_tokens": ["cd", "ef"]},
    ]


def test_segmentation_deterministic_and_ids_consistent():
    cfg = SubwordConfig()
    s1, v1, _ = segment_pairs(_records(), cfg)
    s2, v2, _ = segment_pairs(_records(), cfg)
    assert s1 == s2 and v1 == v2
    vocab_index = {p: i for i, p in enumerate(v1)}
    for pair in s1["pairs"]:
        assert pair["src_ids"] == [vocab_index[p] for p in pair["src_pieces"]]
        assert pair["tgt_ids"] == [vocab_index[p] for p in pair["tgt_pieces"]]
    used = set()
    for pair in s1["pairs"]:
        used.update(pair["src_pieces"])
        used.update(pair["tgt_pieces"])
    assert set(v1) == used  # vocab recomputable from per-sentence segmentations


def test_tie_break_prefers_more_boundaries():
    cfg = SubwordConfig(freq_weight=1.0, len_weight=0.0)
    # log1p(9) == 2 * log1p(3): 2-piece and 4-piece candidates tie on score.
    freq = {"ab": 8, "cd": 8, "a": 2, "b": 2, "c": 2, "d": 2}
    cands = token_candidates("abcd", cfg)
    assert best_candidate(cands, freq, cfg) == ("a", "b", "c", "d")


def test_tie_break_lexicographic_ascending():
    cfg = SubwordConfig(freq_weight=1.0, len_weight=0.0)
    freq = {"ab": 8, "bc": 8, "a": 2, "c": 2, "b": 0}
    cands = token_candidates("abc", cfg)
    # ("a","bc") and ("ab","c") tie on score and boundary count.
    assert best_candidate(cands, freq, cfg) == ("a", "bc")


def test_vocab_empty():
    cfg = SubwordConfig()
    with pytest.raises(PipelineError) as exc:
        segment_pairs([{"pair_id": "p0", "src_tokens": [], "tgt_tokens": []}], cfg)
    assert exc.value.code == Codes.VOCAB_EMPTY


@pytest.mark.parametrize("kwargs", [
    {"min_len": 0},
    {"min_len": 3, "max_len": 2},
    {"approx_top_k": 0},
    {"exact_candidate_cap": 0},
    {"freq_weight": float("nan")},
    {"block_size": 0},
])
def test_config_invalid(kwargs):
    with pytest.raises(PipelineError) as exc:
        SubwordConfig(**kwargs).validate()
    assert exc.value.code == Codes.CONFIG_INVALID
