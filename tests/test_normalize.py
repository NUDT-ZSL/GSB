import pytest

from qu_service.errors import E_EMPTY_QUERY, E_MALFORMED_QUERY, StageError
from qu_service.normalize import normalize_text


def test_fullwidth_and_case_fold():
    result = normalize_text("ＡＰＰＬＥ　手机")
    assert result["text"] == "apple 手机"
    ops = {m["op"] for m in result["modified_positions"]}
    assert "width_fold" in ops and "case_fold" in ops
    assert result["fingerprint"]


def test_whitespace_collapse():
    result = normalize_text("华为   手机\t\t全新")
    assert result["text"] == "华为 手机 全新"
    ws = [m for m in result["modified_positions"] if m["op"] == "ws_collapse"]
    assert ws


def test_punct_strip():
    result = normalize_text("手机！！！多少钱？")
    assert result["text"] == "手机 多少钱" or result["text"] == "手机多少钱"
    assert any(m["op"] == "punct_strip" for m in result["modified_positions"])


def test_empty_query_raises_business_code():
    with pytest.raises(StageError) as exc:
        normalize_text("   ")
    assert exc.value.code == E_EMPTY_QUERY


def test_punct_only_raises_business_code():
    with pytest.raises(StageError) as exc:
        normalize_text("！！！。。。")
    assert exc.value.code == E_EMPTY_QUERY


def test_malformed_none_raises():
    with pytest.raises(StageError) as exc:
        normalize_text(None)
    assert exc.value.code == E_MALFORMED_QUERY


def test_deterministic_fingerprint():
    assert normalize_text("ＡＰＰＬＥ　手机") == normalize_text("ＡＰＰＬＥ　手机")
