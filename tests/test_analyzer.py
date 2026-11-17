from injection_workbench.analyzer import (
    analyze_manifest,
    classify_path,
    E_MISSING_ENC,
    E_ORDER,
    E_MISMATCH,
    MODE_EXACT,
    MODE_BATCH,
)
from injection_workbench.parser import parse_manifest, Sink, PathRecord


def sink(sink_id="s1", families=("html",)):
    return Sink(sink_id=sink_id, line=1, context_type="html-text", required_families=families)


def path(transforms, path_id="p1", sink_id="s1"):
    return PathRecord(path_id=path_id, source_id="src", sink_id=sink_id, transforms=transforms)


def test_missing_encoding_defect():
    _, defect = classify_path(path([]), sink())
    assert defect.error_code == E_MISSING_ENC
    assert defect.defect_index is None
    assert defect.required_families == ["html"]


def test_order_defect_encode_then_decode():
    _, defect = classify_path(path(["html_escape", "html_unescape"]), sink())
    assert defect.error_code == E_ORDER
    assert defect.defect_index == 2


def test_mismatch_defect_wrong_family():
    _, defect = classify_path(path(["sql_quote"]), sink())
    assert defect.error_code == E_MISMATCH
    assert defect.defect_index == 1
    assert defect.family == "sql"


def test_mismatch_defect_wrong_order_multi_family():
    multi_sink = sink(families=("html", "js-string"))
    _, defect = classify_path(path(["js_string_escape", "html_escape"]), multi_sink)
    assert defect.error_code == E_MISMATCH


def test_safe_path_no_defect():
    _, defect = classify_path(path(["html_escape", "htmlspecialchars"]), sink())
    assert defect is None


def test_defects_are_mutually_exclusive_one_per_path():
    # 顺序颠倒优先于缺少编码
    _, defect = classify_path(path(["html_escape", "html_unescape"]), sink())
    assert defect.error_code == E_ORDER


def _manifest_dict():
    return {
        "file_id": "f",
        "dialect": "html",
        "sinks": [{"sink_id": "s1", "line": 1, "context_type": "html-text"}],
        "paths": [
            {"path_id": "p1", "source_id": "a", "sink_id": "s1", "transforms": ["html_escape"]},
            {"path_id": "p2", "source_id": "a", "sink_id": "s1", "transforms": []},
            {"path_id": "p2", "source_id": "a", "sink_id": "s1", "transforms": []},
        ],
    }


def test_sink_aggregation_and_dedup_first_wins(config):
    result = analyze_manifest(parse_manifest(_manifest_dict()), config)
    sink_verdict = result["sink_verdicts"][0]
    assert sink_verdict["verdict"] == "injectable"
    # 去重后唯一路径为 p1/p2，缺陷唯一计数只算一次 p2
    assert sink_verdict["unique_defect_count"] == 1
    assert sink_verdict["duplicate_count"] == 1
    assert sink_verdict["path_ids"] == ["p1", "p2"]


def test_sink_safe_only_when_all_unique_paths_safe(config):
    payload = _manifest_dict()
    payload["paths"] = [
        {"path_id": "p1", "source_id": "a", "sink_id": "s1", "transforms": ["html_escape"]},
        {"path_id": "p2", "source_id": "a", "sink_id": "s1", "transforms": ["htmlspecialchars"]},
    ]
    result = analyze_manifest(parse_manifest(payload), config)
    assert result["sink_verdicts"][0]["verdict"] == "safe"


def test_mode_switch_and_equivalence_pruning(config):
    payload = {
        "file_id": "big",
        "dialect": "html",
        "sinks": [
            {"sink_id": f"s{i}", "line": i + 1, "context_type": "html-text"}
            for i in range(4)
        ],
        "paths": [
            {"path_id": f"p{i}", "source_id": "x", "sink_id": f"s{i % 4}",
             "transforms": ["html_escape"]}
            for i in range(8)
        ],
    }
    result = analyze_manifest(parse_manifest(payload), config)
    assert result["mode"] == MODE_BATCH
    assert result["equivalence_pruning"]["enabled"] is True
    assert result["equivalence_pruning"]["memo_hits"] >= 1
    assert 0 < result["equivalence_pruning"]["prune_ratio"] <= 1


def test_exact_and_batch_modes_identical_results(config):
    payload = _manifest_dict()
    exact = analyze_manifest(parse_manifest(payload), config, force_mode=MODE_EXACT)
    batch = analyze_manifest(parse_manifest(payload), config, force_mode=MODE_BATCH)
    comparable_keys = ("sink_verdicts", "path_verdicts", "rejections")
    for key in comparable_keys:
        assert exact[key] == batch[key]
    assert exact["mode"] == MODE_EXACT and batch["mode"] == MODE_BATCH
