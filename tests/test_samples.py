from __future__ import annotations

from pathlib import Path

import pytest

from ctr_cvr_joint.errors import E_DUP_REQUEST, E_FORMAT_INVALID, BusinessError
from ctr_cvr_joint.samples import (
    apply_label_updates,
    load_samples_jsonl,
    load_samples_parquet,
    order_samples,
    write_samples_jsonl,
    write_samples_parquet,
)


DATA = Path(__file__).resolve().parents[1] / "data"


def test_jsonl_and_parquet_have_identical_rows(fixtures):
    jsonl = fixtures["samples"]
    parquet = load_samples_parquet(DATA / "requests.parquet")
    assert [item.to_dict() for item in jsonl] == [item.to_dict() for item in parquet]
    assert [item.request_id for item in jsonl] == sorted(
        item.request_id for item in jsonl
    )


def test_jsonl_parquet_roundtrip(tmp_path, fixtures):
    jsonl_path = tmp_path / "samples.jsonl"
    parquet_path = tmp_path / "samples.parquet"
    count, checksum = write_samples_jsonl(jsonl_path, fixtures["samples"])
    info = write_samples_parquet(parquet_path, fixtures["samples"])
    assert count == 12
    assert len(checksum) == 64
    assert info["rows"] == 12
    assert [item.to_dict() for item in load_samples_jsonl(jsonl_path)] == [
        item.to_dict() for item in load_samples_parquet(parquet_path)
    ]


def test_duplicate_request_and_label_update(fixtures):
    with pytest.raises(BusinessError) as excinfo:
        order_samples(fixtures["samples"] + [fixtures["samples"][0]])
    assert excinfo.value.code == E_DUP_REQUEST
    updated = apply_label_updates(
        fixtures["samples"],
        {"req-0002": {"label_click": 0, "label_convert": 0}},
    )
    sample = next(item for item in updated if item.request_id == "req-0002")
    assert sample.label_click == 0


def test_invalid_sample_reports_field(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"request_id":"r1","timestamp_ms":1,"user_id":"u","item_id":"i",'
        '"numeric_feats":{},"categorical_feats":{},"label_click":2,"label_convert":0}\n',
        encoding="utf-8",
    )
    with pytest.raises(BusinessError) as excinfo:
        load_samples_jsonl(path)
    error = excinfo.value
    assert error.code == E_FORMAT_INVALID
    assert error.line == 1
    assert error.field == "label_click"

