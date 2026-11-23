from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ctr_cvr_joint.errors import BusinessError, E_FORMAT_INVALID, E_VERSION_UNSUPPORTED
from ctr_cvr_joint.hashing import sha256_bytes
from ctr_cvr_joint.specs import load_spec_bytes, normalize_spec


DATA = Path(__file__).resolve().parents[1] / "data"


def test_reads_all_three_versions_with_provenance(fixtures):
    for key, version in (("spec_v1", 1), ("spec_v2", 2), ("spec_v3", 3)):
        loaded = fixtures[key]
        assert loaded.version == version
        assert len(loaded.checksum) == 64
        assert loaded.line_summaries
        raw_name = "spec_v1.txt" if version == 1 else f"spec_v{version}.json"
        assert loaded.checksum == sha256_bytes((DATA / raw_name).read_bytes())
        assert all(item.checksum and item.preview for item in loaded.line_summaries)


def test_field_set_dispatch_without_explicit_headers():
    v1 = b"N|ctr_base|0.4|0.1|0.5\nS|ctr|0.2|2\nS|cvr|0.1|2\n"
    assert load_spec_bytes(v1).version == 1

    v2 = json.loads((DATA / "spec_v2.json").read_text(encoding="utf-8"))
    v2.pop("format_version")
    assert load_spec_bytes(json.dumps(v2).encode("utf-8")).version == 2

    v3 = json.loads((DATA / "spec_v3.json").read_text(encoding="utf-8"))
    v3.pop("format_version")
    assert load_spec_bytes(json.dumps(v3).encode("utf-8")).version == 3


def test_unknown_version_uses_business_code():
    with pytest.raises(BusinessError) as excinfo:
        load_spec_bytes(b"version=9\nN|a|0|1|1\nS|ctr|.2|1\nS|cvr|.1|1\n")
    assert excinfo.value.code == E_VERSION_UNSUPPORTED
    assert excinfo.value.details["version"] == 9

    with pytest.raises(BusinessError) as excinfo:
        load_spec_bytes(b'{"format_version":99}')
    assert excinfo.value.code == E_VERSION_UNSUPPORTED


def test_v1_pipe_field_count_reports_line_and_field():
    with pytest.raises(BusinessError) as excinfo:
        load_spec_bytes(b"version=1\nN|a|0|1\nS|ctr|.2|1\nS|cvr|.1|1\n")
    error = excinfo.value
    assert error.code == E_FORMAT_INVALID
    assert error.line == 2
    assert error.field == "columns"
    assert error.details["expected"] == 5


def test_v1_numeric_parse_error_reports_line_and_field():
    with pytest.raises(BusinessError) as excinfo:
        load_spec_bytes(b"version=1\nN|a|bad|1|1\nS|ctr|.2|1\nS|cvr|.1|1\n")
    error = excinfo.value
    assert error.code == E_FORMAT_INVALID
    assert error.line == 2
    assert error.field == "numeric.a.mean"


def test_v2_missing_key_reports_path():
    payload = json.loads((DATA / "spec_v2.json").read_text(encoding="utf-8"))
    broken = copy.deepcopy(payload)
    broken["features"]["numeric"][0].pop("mean")
    with pytest.raises(BusinessError) as excinfo:
        load_spec_bytes(json.dumps(broken).encode("utf-8"))
    error = excinfo.value
    assert error.code == E_FORMAT_INVALID
    assert "mean" in error.field
    assert error.line >= 1


def test_v3_nested_default_and_semantics_are_read(fixtures):
    normalized = normalize_spec(fixtures["spec_v3"].spec)
    assert normalized.numeric_map()["ctr_base"].default == 0.42
    assert normalized.categorical_map()["device"].default == "unknown"
    assert normalized.stat_map()["ctr"].semantics_name == "snapshot_bayes_decay"
    assert normalized.stat_map()["ctr"].semantics_version == 3

