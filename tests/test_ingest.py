import json

import pytest

from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.ingest import IngestConfig, run_ingest

SRC_VOCAB = ["s0001", "s0002", "s0003"]
TGT_VOCAB = ["t0001", "t0002", "t0003"]


def _rec(pair_id, src=("s0001", "s0002"), tgt=("t0001",),
         align=((0, 0), (1, 0)), noise=0.0, domain="news"):
    return {
        "pair_id": pair_id,
        "src_tokens": list(src),
        "tgt_tokens": list(tgt),
        "align": [list(a) for a in align],
        "src_len": len(src),
        "tgt_len": len(tgt),
        "noise_level": noise,
        "domain_tag": domain,
    }


def _write(tmp_path, records):
    path = tmp_path / "corpus.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return path


def _run(tmp_path, records):
    return run_ingest(_write(tmp_path, records), SRC_VOCAB, TGT_VOCAB, 1,
                      IngestConfig(shard_size=2), "cfg")


def test_sorted_ingest_and_summary_deterministic(tmp_path):
    records = [_rec("p002"), _rec("p000"), _rec("p001")]
    s1, r1 = _run(tmp_path, records)
    s2, r2 = _run(tmp_path, records)
    assert [r["pair_id"] for r in r1] == ["p000", "p001", "p002"]
    assert s1 == s2
    assert s1["shard_count"] == 2
    assert s1["pairs"][0]["shard"] == 0
    assert s1["pairs"][0]["codepoint_interval"][0] <= s1["pairs"][0]["codepoint_interval"][1]


def test_duplicate_pair_id(tmp_path):
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [_rec("p000"), _rec("p000")])
    assert exc.value.code == Codes.DUP_PAIR
    assert exc.value.pair_id == "p000"


def test_empty_sentence(tmp_path):
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [_rec("p000", src=(), align=())])
    assert exc.value.code == Codes.EMPTY_SENT
    assert exc.value.pair_id == "p000"


def test_align_out_of_range(tmp_path):
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [_rec("p000", align=((0, 0), (1, 5)))])
    assert exc.value.code == Codes.ALIGN_RANGE
    assert exc.value.pair_id == "p000"
    assert exc.value.position == 1


def test_align_direction_illegal(tmp_path):
    bad = _rec("p000", src=("s0001", "s0002", "s0003"),
               align=((1, 0), (0, 0)))
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [bad])
    assert exc.value.code == Codes.ALIGN_RANGE
    assert exc.value.position == 1


def test_token_outside_controlled_vocab(tmp_path):
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [_rec("p000", src=("s0001", "s9999"))])
    assert exc.value.code == Codes.VOCAB_TOKEN
    assert exc.value.position == 1


def test_declared_length_mismatch(tmp_path):
    rec = _rec("p000")
    rec["src_len"] = 99
    with pytest.raises(PipelineError) as exc:
        _run(tmp_path, [rec])
    assert exc.value.code == Codes.LEN_MISMATCH
