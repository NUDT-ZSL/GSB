from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ROOT, run_cli
from stream_reopt.errors import EngineError
from stream_reopt.sample import load_sample


_CASES = [
    (case["file"], case["expected_error"])
    for case in json.loads(
        (ROOT / "samples" / "anomaly_manifest.json").read_text(encoding="utf-8")
    )["cases"]
]


@pytest.mark.parametrize(("relative_path", "expected_code"), _CASES)
def test_anomaly_manifest_codes(relative_path: str, expected_code: str) -> None:
    with pytest.raises(EngineError) as captured:
        load_sample(ROOT / relative_path)
    assert captured.value.code == expected_code


def test_invalid_sample_does_not_create_database(tmp_path: Path) -> None:
    database = tmp_path / "must-not-exist.db"
    code, payload, completed = (
        lambda result: (result.returncode, json.loads(result.stdout), result)
    )(run_cli(["run", "--sample", "samples/invalid/bad_ndv.json", "--db", str(database)], cwd=ROOT))
    assert code == 2
    assert payload["error"]["code"] == "E_BAD_TABLE"
    assert not database.exists()
    assert completed.stderr == ""


def test_valid_sample_snapshot_shape() -> None:
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    assert sample.sample_id == "reopt_demo"
    assert [table.name for table in sample.tables] == ["customers", "orders", "lineitems"]
    assert len(sample.plan.batches) == 9
    assert sample.statistics.tables["orders"].columns["product_id"].ndv == 256
    assert sample.plan.initial_shape.join_order == ("lineitems", "orders", "customers")
