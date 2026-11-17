from injection_workbench.normalizer import normalize


def test_merge_consecutive_same_family():
    chain = normalize(["html_escape", "htmlspecialchars"])
    assert len(chain.steps) == 1
    assert chain.steps[0].family == "html"
    assert chain.steps[0].indices == [1, 2]
    assert chain.cancels == []


def test_encode_decode_cancel_keeps_original_indices():
    chain = normalize(["html_escape", "html_unescape"])
    assert chain.steps == []
    assert len(chain.cancels) == 1
    record = chain.cancels[0]
    assert record.family == "html"
    assert record.encode_indices == [1]
    assert record.decode_indices == [2]
    assert record.is_encode_then_decode() is True
    assert record.revert_index() == 2


def test_distinct_families_remain_ordered():
    chain = normalize(["html_escape", "js_string_escape"])
    assert [step.family for step in chain.steps] == ["html", "js-string"]
    assert chain.signature == "html:encode>js-string:encode"


def test_inner_cancel_with_outer_surviving():
    chain = normalize(["html_escape", "js_string_escape", "js_string_unescape"])
    assert [step.label() for step in chain.steps] == ["html:encode"]
    assert chain.cancels[0].family == "js-string"


def test_decode_then_encode_is_still_cancel_not_order_defect():
    chain = normalize(["html_unescape", "html_escape"])
    assert chain.steps == []
    assert chain.cancels[0].is_encode_then_decode() is False
