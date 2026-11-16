from qu_service.errors import E_SLOT_OVERLAP
from qu_service.slots import extract_slots

DICTS = [
    {"type": "brand", "priority": 1, "entries": ["苹果", "华为"]},
    {"type": "category", "priority": 2, "entries": ["手机", "手机壳", "苹果干", "笔记本电脑", "笔记本"]},
    {"type": "attribute", "priority": 3, "entries": ["全新", "红色"]},
]


def test_longest_match_wins_and_suppressed_recorded():
    result = extract_slots("华为手机壳 全新", DICTS)
    entries = [s["entry"] for s in result["slots"]]
    assert "手机壳" in entries and "手机" not in entries
    suppressed = [s["entry"] for s in result["suppressed"]]
    assert "手机" in suppressed
    assert result["overlap_count"] == len(result["suppressed"])
    assert E_SLOT_OVERLAP in result["codes"]


def test_brand_vs_category_overlap():
    result = extract_slots("苹果干", DICTS)
    entries = [s["entry"] for s in result["slots"]]
    assert entries == ["苹果干"]  # 最长优先，品牌“苹果”被吞并
    assert result["suppressed"][0]["entry"] == "苹果"
    assert result["suppressed"][0]["suppressed_by"] == "苹果干"


def test_same_length_priority_then_start():
    dicts = [
        {"type": "brand", "priority": 1, "entries": ["ab"]},
        {"type": "category", "priority": 2, "entries": ["ab"]},
    ]
    result = extract_slots("ab", dicts)
    assert result["slots"][0]["type"] == "brand"  # 同长同位置按词典优先级
    assert result["suppressed"][0]["type"] == "category"


def test_no_overlap_no_code():
    result = extract_slots("华为 全新", DICTS)
    assert result["overlap_count"] == 0
    assert result["codes"] == []


def test_slots_sorted_by_position():
    result = extract_slots("全新 华为 手机", DICTS)
    starts = [s["start"] for s in result["slots"]]
    assert starts == sorted(starts)
