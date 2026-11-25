from __future__ import annotations

import json

import pytest

from dedup_workbench.config import parse_config
from dedup_workbench.engine import DedupWorkbench
from dedup_workbench.errors import WorkbenchError
from dedup_workbench.ingest import content_hash_for

from dedup_workbench.testing import base_config, make_record


def run_engine(tmp_path, records, config=None):
    input_path = tmp_path / "corpus.jsonl"
    input_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    config = config or base_config()
    return DedupWorkbench(tmp_path / "runtime", config).run(input_path)


def test_duplicate_doc_id_has_structured_code_and_field(tmp_path):
    records = [
        make_record("doc-1", "first valid text"),
        make_record("doc-1", "second valid text"),
    ]
    with pytest.raises(WorkbenchError) as exc_info:
        run_engine(tmp_path, records)
    assert exc_info.value.code == "E_DUPLICATE_DOC_ID"
    assert exc_info.value.field == "doc_id"
    assert len(exc_info.value.details["occurrences"]) == 2


def test_content_hash_mismatch_has_expected_and_provided(tmp_path):
    record = make_record("doc-1", "hash me correctly")
    record["content_hash"] = "0" * 64
    with pytest.raises(WorkbenchError) as exc_info:
        run_engine(tmp_path, [record])
    assert exc_info.value.code == "E_CONTENT_HASH_MISMATCH"
    assert exc_info.value.field == "content_hash"
    assert exc_info.value.details["expected"] == content_hash_for("hash me correctly")


def test_invalid_language_uses_controlled_set(tmp_path):
    record = make_record("doc-1", "Lang check", lang="klingon")
    with pytest.raises(WorkbenchError) as exc_info:
        run_engine(tmp_path, [record])
    assert exc_info.value.code == "E_LANG_NOT_ALLOWED"
    assert exc_info.value.field == "lang"
    assert "en" in exc_info.value.details["allowed"]


def test_empty_normalized_text_error(tmp_path):
    text = " \n\t "
    record = make_record("doc-1", text, content_hash=content_hash_for(text))
    with pytest.raises(WorkbenchError) as exc_info:
        run_engine(tmp_path, [record])
    assert exc_info.value.code == "E_EMPTY_TEXT"
    assert exc_info.value.field == "text"


@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        ({"similarity_threshold": 1.2}, "similarity_threshold"),
        ({"candidate_window": 0}, "candidate_window"),
        ({"chunk_signature_length": True}, "chunk_signature_length"),
        ({"validation_mode": "sampled"}, "validation_mode"),
        ({"config_version": 0}, "config_version"),
    ],
)
def test_invalid_config_points_to_field(mutation, field):
    config = base_config(**mutation)
    with pytest.raises(WorkbenchError) as exc_info:
        parse_config(config)
    assert exc_info.value.code == "E_CONFIG_INVALID"
    assert exc_info.value.field == field


def test_jsonl_parse_error_includes_line(tmp_path):
    input_path = tmp_path / "bad.jsonl"
    input_path.write_text('\n{bad json}\n', encoding="utf-8")
    with pytest.raises(WorkbenchError) as exc_info:
        DedupWorkbench(tmp_path / "runtime", base_config()).run(input_path)
    assert exc_info.value.code == "E_JSONL_PARSE"
    assert "2" in exc_info.value.field
