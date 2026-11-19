from __future__ import annotations

from pathlib import Path

import pytest

from qrw.errors import BusinessError
from qrw.readers import detect_dialect, read_queries, read_template_file, read_template_text


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_unknown_and_truncated_version_header(tmp_path: Path, slot_types, config):
    unknown = _write(tmp_path / "unknown.txt", "#V9\n{city}|x|1\n")
    with pytest.raises(BusinessError) as exc:
        read_template_file(unknown, slot_types, config)
    assert exc.value.code == "E_VERSION_UNSUPPORTED"
    assert exc.value.line == 1
    assert exc.value.field == "header"

    truncated = _write(tmp_path / "truncated.txt", "#V\n{city}|x|1\n")
    with pytest.raises(BusinessError) as exc:
        read_template_file(truncated, slot_types, config)
    assert exc.value.code == "E_FORMAT_CORRUPT"
    assert exc.value.line == 1
    assert exc.value.field == "header"


def test_v1_pipe_count_has_line_and_segment(tmp_path: Path, slot_types, config):
    source = _write(tmp_path / "bad.txt", "#V1\n{city} 酒店 推荐|{city} 榜单\n")
    with pytest.raises(BusinessError) as exc:
        read_template_file(source, slot_types, config)
    assert exc.value.code == "E_FORMAT_CORRUPT"
    assert exc.value.line == 2
    assert exc.value.field == "segment[3]"
    assert exc.value.details["actual_count"] == 2


def test_v2_missing_required_field_location(tmp_path: Path, slot_types, config):
    source = _write(
        tmp_path / "bad_v2.txt",
        '#V2\nid="R1"; replacement="{city} 榜单"; slots="city:GEO"; weight=1; enabled=true; max_depth=1; order=10\n',
    )
    with pytest.raises(BusinessError) as exc:
        read_template_file(source, slot_types, config)
    assert exc.value.code == "E_FORMAT_CORRUPT"
    assert exc.value.line == 2
    assert exc.value.field == "pattern"


def test_duplicate_query_and_empty_pattern(samples: Path, slot_types, config):
    with pytest.raises(BusinessError) as exc:
        read_queries(samples / "anomalies" / "duplicate_queries.jsonl")
    assert exc.value.code == "E_DUP_QUERY"
    assert exc.value.line == 2
    assert exc.value.details["first_line"] == 1

    with pytest.raises(BusinessError) as exc:
        read_template_file(samples / "anomalies" / "empty_pattern.txt", slot_types, config)
    assert exc.value.code == "E_PATTERN_EMPTY"
    assert exc.value.line == 2
    assert exc.value.field == "pattern"


def test_v3_field_set_dispatch_without_version(slot_types, config):
    text = '{"rules":[{"id":"R","pattern":"{city}","slots":[{"name":"city","type":"GEO"}],"replacement":[{"text":"{city}"}],"weight":1,"enabled":true,"max_depth":1,"priority":1}]}'
    assert detect_dialect(text) == "v3"
    parsed = read_template_text(text, "<memory>", slot_types, config)
    assert parsed.dialect == "v3"
    assert parsed.rules[0].rule_id == "R"


def test_v1_infers_slots_and_defaults(samples: Path, slot_types, config):
    parsed = read_template_file(samples / "v1.txt", slot_types, config)
    assert parsed.dialect == "v1"
    assert parsed.rules[0].slots[0].declared is False
    fields = {fill.field for fill in parsed.rules[0].fills}
    assert "weight" in fields
    assert "enabled" in fields
    assert "max_depth" in fields
    assert any(field.startswith("slots.") for field in fields)

