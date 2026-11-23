from __future__ import annotations

import json
from pathlib import Path

import pytest

from slowquery.errors import EngineError
from slowquery.samples import validate_samples_document
from slowquery.sql import normalize_sql

from .conftest import invoke, invoke_json


def test_literal_case_whitespace_and_in_list_normalization() -> None:
    left = normalize_sql(
        "SELECT id, amount FROM orders "
        "WHERE customer_id = 42 AND status IN ('new','paid')"
    )
    right = normalize_sql(
        " select id,amount\nfrom ORDERS where customer_id=7 and status in ('paid') ; "
    )
    assert left["template_id"] == right["template_id"]
    assert left["canonical"] == right["canonical"]
    assert normalize_sql(
        "SELECT id, amount FROM orders WHERE customer_id = 42 AND status IN ('new','paid')"
    )["canonical"] == left["canonical"]


def test_different_structure_gets_different_template() -> None:
    first = normalize_sql("SELECT id FROM orders WHERE status = 'new'")
    second = normalize_sql("SELECT amount FROM orders WHERE status = 'new'")
    assert first["template_id"] != second["template_id"]


def test_invalid_sql_is_a_business_error() -> None:
    with pytest.raises(EngineError) as exc:
        normalize_sql("DELETE FROM orders WHERE id = 1")
    assert exc.value.code == "ERR_SQL_PARSE"


def test_duplicate_sequence_rejected_and_load_writes_no_state(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "seq": 1,
                        "logical_clock_ms": 1,
                        "sql": "SELECT * FROM orders WHERE id = 1",
                        "duration_ms": 1,
                        "scanned_rows": 1,
                        "returned_rows": 1,
                    },
                    {
                        "seq": 1,
                        "logical_clock_ms": 2,
                        "sql": "SELECT * FROM orders WHERE id = 2",
                        "duration_ms": 1,
                        "scanned_rows": 1,
                        "returned_rows": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "new" / "state.db"
    result = invoke(
        state,
        ["load", "--reset", "--samples", str(invalid)],
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "ERR_SAMPLE_SEQ"
    assert not state.exists()


def test_negative_metric_rejected_before_state_write(tmp_path: Path) -> None:
    document = {
        "samples": [
            {
                "seq": 1,
                "logical_clock_ms": 1,
                "sql": "SELECT * FROM orders WHERE id = 1",
                "duration_ms": -1,
                "scanned_rows": 1,
                "returned_rows": 1,
            }
        ]
    }
    with pytest.raises(EngineError) as exc:
        validate_samples_document(document)
    assert exc.value.code == "ERR_SAMPLE_METRIC"


def test_stateful_conflict_does_not_advance_watermark(sampled: Path) -> None:
    status_before = invoke_json(sampled, ["status"])
    assert status_before["state"]["sample_watermark"]["last_seq"] == 12
    # Replace an already committed sample and append a new one.  Preflight must
    # reject the stream before the appended sample is written.
    conflict = sampled.parent / "conflict.json"
    conflict.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "seq": 12,
                        "logical_clock_ms": 2700,
                        "sql": "SELECT id FROM orders WHERE id = 999",
                        "duration_ms": 1,
                        "scanned_rows": 1,
                        "returned_rows": 1,
                    },
                    {
                        "seq": 13,
                        "logical_clock_ms": 2800,
                        "sql": "SELECT id FROM orders WHERE id = 1000",
                        "duration_ms": 1,
                        "scanned_rows": 1,
                        "returned_rows": 1,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    result = invoke(
        sampled, ["sample", "--samples", str(conflict)], check=False
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "ERR_SAMPLE_CONFLICT"
    status_after = invoke_json(sampled, ["status"])
    assert status_after["state"]["sample_watermark"]["last_seq"] == 12
    assert status_after["aggregates"] == status_before["aggregates"]
