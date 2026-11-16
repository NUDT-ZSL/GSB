from qu_service.injection import (InjectionPlan, apply_to_dicts,
                                  apply_to_queries, select_malformed)


def test_malformed_selection_deterministic():
    ids = [f"Q{i:03d}" for i in range(1, 25)]
    a = select_malformed(ids, 0.12)
    b = select_malformed(list(reversed(ids)), 0.12)
    assert a == b  # 与输入顺序无关
    assert 0 < len(a) < len(ids)


def test_zero_ratio_selects_nothing():
    assert select_malformed(["Q001"], 0.0) == []


def test_apply_to_queries_marks_hits():
    plan = InjectionPlan({"malformed": {"ratio": 0.12}, "dict_remove": ["手机壳"]})
    queries = [{"query_id": "Q006", "text": "ＡＰＰＬＥ　手机壳，全新"},
               {"query_id": "Q007", "text": "iPhone 15 手机壳 红色"}]
    apply_to_queries(queries, plan)
    q6, q7 = queries
    assert "INJ_DICT_REMOVE:手机壳" in q6["injections"]
    assert q7["text"] is None and "INJ_MALFORMED" in q7["injections"]


def test_dict_removal():
    plan = InjectionPlan({"dict_remove": ["手机壳"]})
    dicts = [{"type": "category", "priority": 2, "entries": ["手机", "手机壳"]}]
    out = apply_to_dicts(dicts, plan)
    assert out[0]["entries"] == ["手机"]
    assert dicts[0]["entries"] == ["手机", "手机壳"]  # 原对象不被修改
