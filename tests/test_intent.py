import json
from pathlib import Path

from qu_service.errors import E_SCORE_OUT_OF_RANGE
from qu_service.intent import score_intents

ROOT = Path(__file__).resolve().parent.parent
VOCAB = json.loads((ROOT / "data" / "intent_vocab.json").read_text(encoding="utf-8"))
RULES = json.loads((ROOT / "data" / "rewrite_rules.json").read_text(encoding="utf-8"))["rules"]


def test_scores_l2_normalized():
    result = score_intents("买华为手机 全新", VOCAB, RULES)
    norm = sum(v * v for v in result["scores"].values()) ** 0.5
    assert abs(norm - 1.0) < 1e-6
    assert all(0.0 <= v <= 1.0 for v in result["scores"].values())
    assert result["primary_intent"] == "purchase"


def test_tie_break_lexicographic():
    # 构造完全同分：无任何特征命中时所有类目同分 0
    result = score_intents("zzzqqq", VOCAB, RULES)
    assert result["primary_intent"] == sorted(i["name"] for i in VOCAB["intents"])[0]
    assert result["no_signal"] is True


def test_position_decay_prefers_early_hit():
    # 同一组特征、不同命中位置：归一化后靠前命中的意图得分更高
    early = score_intents("买 便宜", VOCAB, RULES)
    late = score_intents("便宜 买", VOCAB, RULES)
    assert early["scores"]["purchase"] > late["scores"]["purchase"]


def test_rule_intent_boost():
    boosted = score_intents("下单 便宜", VOCAB, RULES)
    plain = score_intents("下单 便宜", VOCAB, [])
    assert boosted["scores"]["purchase"] > plain["scores"]["purchase"]


def test_score_out_of_range_clamped_and_coded():
    result = score_intents("索尼耳机 多少钱", VOCAB, RULES,
                           forced_scores={"purchase": 1.75})
    assert E_SCORE_OUT_OF_RANGE in result["codes"]
    assert all(0.0 <= v <= 1.0 for v in result["scores"].values())


def test_determinism():
    a = score_intents("华为手机和三星手机哪个好", VOCAB, RULES)
    b = score_intents("华为手机和三星手机哪个好", VOCAB, RULES)
    assert a == b
