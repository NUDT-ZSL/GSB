import pytest

from mt_pipeline.consistency import (DIVERGENCE_PRUNING_MISS, adjudicate,
                                     classify_divergence)
from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.pipeline import _run_path
from mt_pipeline.filtering import FilterConfig
from mt_pipeline.subword import SubwordConfig, build_piece_freq


def _records():
    """Synthetic corpus where exact (max-split) and approx(top_k=1) paths
    produce different subword length ratios for the same pair."""
    return [{"pair_id": "p000", "src_tokens": ["abcde"], "tgt_tokens": ["wxyz"],
             "noise_level": 0.0, "domain_tag": "news"}]


def _configs():
    sub_cfg = SubwordConfig(min_len=1, max_len=2, freq_weight=5.0,
                            len_weight=0.0, approx_top_k=1)
    filter_cfg = FilterConfig(ratio_min=0.75, ratio_max=2.0, noise_max=1.0,
                              strategy="strict")
    return sub_cfg, filter_cfg


def test_pruning_miss_classification():
    sub_cfg, _ = _configs()
    records = _records()
    freq = build_piece_freq(
        (t for r in records for t in (*r["src_tokens"], *r["tgt_tokens"])), sub_cfg)
    assert classify_divergence("abcde", sub_cfg, freq) == DIVERGENCE_PRUNING_MISS


def test_paths_agree_when_pruning_keeps_winner():
    sub_cfg, filter_cfg = _configs()
    sub_cfg = SubwordConfig(min_len=1, max_len=2, freq_weight=5.0,
                            len_weight=0.0, approx_top_k=8)
    records = _records()
    e_seg, e_vocab, e_freq, e_det = _run_path(records, sub_cfg, filter_cfg, {}, "exact")
    a_seg, a_vocab, _, a_det = _run_path(records, sub_cfg, filter_cfg, {}, "approx")
    e_kept = [d["pair_id"] for d in e_det if d["decision"] == "keep"]
    a_kept = [d["pair_id"] for d in a_det if d["decision"] == "keep"]
    report = adjudicate(records, e_seg["pairs"], a_seg["pairs"], e_kept, a_kept,
                        sub_cfg, e_freq, len(e_vocab), len(a_vocab))
    assert report["seg_agreement_rate"] == 1.0
    assert report["kept_set_diff"] == []


def test_path_divergence_raises_with_first_pair_and_type():
    sub_cfg, filter_cfg = _configs()
    records = _records()
    e_seg, e_vocab, e_freq, e_det = _run_path(records, sub_cfg, filter_cfg, {}, "exact")
    a_seg, a_vocab, _, a_det = _run_path(records, sub_cfg, filter_cfg, {}, "approx")
    e_kept = [d["pair_id"] for d in e_det if d["decision"] == "keep"]
    a_kept = [d["pair_id"] for d in a_det if d["decision"] == "keep"]
    assert e_kept != a_kept  # sanity: the fixture really diverges
    with pytest.raises(PipelineError) as exc:
        adjudicate(records, e_seg["pairs"], a_seg["pairs"], e_kept, a_kept,
                   sub_cfg, e_freq, len(e_vocab), len(a_vocab))
    assert exc.value.code == Codes.PATH_DIVERGENCE
    assert exc.value.pair_id == "p000"
    assert exc.value.detail["divergence_type"] == DIVERGENCE_PRUNING_MISS
    spectrum = exc.value.detail["report"]
    assert spectrum["divergence_count"] == len(spectrum["divergences"])
