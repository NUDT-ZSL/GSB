from injection_workbench.parser import (
    parse_manifest,
    E_BAD_DIALECT, E_BAD_LINE, E_UNKNOWN_TRANSFORM, E_DUP_SINK_ID,
    E_DUP_PATH_ID, E_UNKNOWN_SINK_REF, E_BAD_JSON,
)


def test_invalid_records_are_rejected_but_processing_continues():
    payload = {
        "file_id": "f",
        "dialect": "cpp",
        "sinks": [
            {"sink_id": "ok", "line": 1, "context_type": "html-text"},
            {"sink_id": "bad", "line": 0, "context_type": "html-text"},
            {"sink_id": "ok", "line": 2, "context_type": "html-text"},
        ],
        "paths": [
            {"path_id": "p1", "source_id": "a", "sink_id": "ok", "transforms": ["nope"]},
            {"path_id": "p2", "source_id": "a", "sink_id": "ghost", "transforms": []},
            {"path_id": "p3", "source_id": "a", "sink_id": "ok", "transforms": []},
            {"path_id": "p3", "source_id": "a", "sink_id": "ok", "transforms": []},
        ],
    }
    parsed = parse_manifest(payload)
    codes = {item.error_code for item in parsed.rejections}
    assert E_BAD_DIALECT in codes
    assert E_BAD_LINE in codes
    assert E_DUP_SINK_ID in codes
    assert E_UNKNOWN_TRANSFORM in codes
    assert E_UNKNOWN_SINK_REF in codes
    assert E_DUP_PATH_ID in codes
    # 合法记录仍被保留；重复路径保留记录并附带 E_DUP_PATH_ID，由分析层去重
    assert [s.sink_id for s in parsed.sinks] == ["ok"]
    assert [p.path_id for p in parsed.paths] == ["p3", "p3"]


def test_rejection_contains_record_index():
    parsed = parse_manifest({
        "file_id": "f", "dialect": "html",
        "sinks": [{"sink_id": "s", "line": -9, "context_type": "html-text"}],
        "paths": [],
    })
    rejection = parsed.rejections[0]
    assert rejection.record_index == 1
    assert rejection.error_code == E_BAD_LINE


def test_bad_json_returns_fatal_rejection(tmp_path):
    from injection_workbench.parser import load_manifest
    target = tmp_path / "broken.json"
    target.write_text("{not json", encoding="utf-8")
    parsed, fatal = load_manifest(target)
    assert parsed is None
    assert fatal.error_code == E_BAD_JSON
