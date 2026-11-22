from __future__ import annotations

import pytest

from ner_governance.errors import (
    E_TYPE_SET_CORRUPT,
    E_VERSION_UNSUPPORTED,
    GovernanceError,
)
from ner_governance.typesets import load_type_set, read_type_set_text


def _code_and_details(callable_):
    with pytest.raises(GovernanceError) as raised:
        callable_()
    return raised.value.code, raised.value.details


def test_three_sample_formats_are_equivalent(sample_paths):
    views = {
        name: load_type_set(sample_paths[name]) for name in ("v1", "v2", "v3")
    }
    assert len({value.semantic_fingerprint() for value in views.values()}) == 1
    assert views["v1"].active_type_names() == ["GPE", "ORG", "PERSON"]
    assert views["v2"].retired_type_names() == ["LOCATION"]
    assert views["v3"].active_alias_map() == views["v1"].active_alias_map()


def test_unknown_header_version(sample_paths):
    code, details = _code_and_details(
        lambda: read_type_set_text("#NER_TYPE_SET|v9|ls-x\nTYPE|PERSON|true\n")
    )
    assert code == E_VERSION_UNSUPPORTED
    assert details["field"] == "version"
    assert details["line"] == 1


def test_truncated_header():
    code, details = _code_and_details(
        lambda: read_type_set_text("#NER_TYPE_SET|v1\n")
    )
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "header"
    assert details["line"] == 1
    assert details["reason"] == "header_truncated"


def test_v1_pipe_field_count_reports_line():
    code, details = _code_and_details(
        lambda: read_type_set_text(
            "#NER_TYPE_SET|v1|ls-x\nTYPE|PERSON\ntrue\n"
        )
    )
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "columns"
    assert details["line"] == 2
    assert details["expected"] == 3


def test_v1_bad_enabled_marker_reports_line():
    code, details = _code_and_details(
        lambda: read_type_set_text(
            "#NER_TYPE_SET|v1|ls-x\nTYPE|PERSON|maybe\n"
        )
    )
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "enabled"
    assert details["line"] == 2


def test_v2_missing_required_field():
    text = (
        "#NER_TYPE_SET|v2|ls-x\n"
        "type=PERSON\n"
        "bio_prefix_required=true\n"
        "type=ORG\n"
        "bio_prefix_required=true\n"
        "enabled=true\n"
    )
    code, details = _code_and_details(lambda: read_type_set_text(text))
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "enabled"
    assert details["line"] == 2
    assert details["reason"] == "required_field_missing"


def test_v3_missing_field_has_line_and_field():
    text = (
        "{\n"
        '  "format": "NER_TYPE_SET_V3",\n'
        '  "label_set_version": "ls-x",\n'
        '  "types": [{"name": "PERSON", "enabled": true}]\n'
        "}\n"
    )
    code, details = _code_and_details(lambda: read_type_set_text(text))
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "types[0].bio_prefix_required"
    assert details["line"] == 4


def test_json_syntax_error_line():
    code, details = _code_and_details(
        lambda: read_type_set_text('{"format": "NER_TYPE_SET_V3",')
    )
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "$"
    assert details["line"] >= 1


def test_headerless_field_dispatch():
    v1 = read_type_set_text(
        "META|label_set_version|ls-x\nTYPE|PERSON|true\n"
    )
    v2 = read_type_set_text(
        "label_set_version=ls-x\n"
        "type=PERSON\n"
        "bio_prefix_required=true\n"
        "enabled=true\n"
    )
    v3 = read_type_set_text(
        '{"label_set_version":"ls-x","types":[{"name":"PERSON",'
        '"bio_prefix_required":true,"enabled":true}]}'
    )
    assert {v1.format_version, v2.format_version, v3.format_version} == {
        "v1",
        "v2",
        "v3",
    }
    assert len(
        {
            v1.semantic_fingerprint(),
            v2.semantic_fingerprint(),
            v3.semantic_fingerprint(),
        }
    ) == 1


def test_alias_target_must_exist():
    code, details = _code_and_details(
        lambda: read_type_set_text(
            "#NER_TYPE_SET|v1|ls-x\n"
            "TYPE|PERSON|true\n"
            "ALIAS|PER|MISSING\n"
        )
    )
    assert code == E_TYPE_SET_CORRUPT
    assert details["field"] == "target"
    assert details["line"] == 3
