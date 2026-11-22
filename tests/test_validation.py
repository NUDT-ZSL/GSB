from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from stream_reopt.errors import BusinessError, ErrorCode
from stream_reopt.models import parse_scenario

from .conftest import run_cli, write_json


def _bad_duplicate_table(data: dict) -> None:
    data["tables"][1]["name"] = data["tables"][0]["name"]


def _bad_ndv(data: dict) -> None:
    data["tables"][0]["columns"][0]["ndv"] = data["tables"][0]["rows"] + 1


def _bad_width(data: dict) -> None:
    data["tables"][0]["columns"][0]["width"] = 0


def _bad_join_column(data: dict) -> None:
    data["plan"]["predicates"][0]["left_column"] = "missing_column"


def _bad_type(data: dict) -> None:
    data["tables"][0]["columns"][0]["type"] = "text"


def _bad_batch_order(data: dict) -> None:
    data["plan"]["batch_order"][-1] = data["plan"]["batch_order"][0]


def _bad_batch_rows(data: dict) -> None:
    data["plan"]["batch_rows"]["orders"][3] = 0


def _bad_selectivity(data: dict) -> None:
    data["initial_stats"]["join_selectivities"]["orders_customer_fk"]["selectivity"] = 0.0


def _bad_budget(data: dict) -> None:
    data["config"]["switch_cost_budget"] = -1


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (_bad_duplicate_table, ErrorCode.BAD_PLAN),
        (_bad_ndv, ErrorCode.BAD_PLAN),
        (_bad_width, ErrorCode.BAD_PLAN),
        (_bad_join_column, ErrorCode.BAD_PLAN),
        (_bad_type, ErrorCode.TYPE_MISMATCH),
        (_bad_batch_order, ErrorCode.BAD_PLAN),
        (_bad_batch_rows, ErrorCode.BAD_PLAN),
        (_bad_selectivity, ErrorCode.BAD_STATS),
        (_bad_budget, ErrorCode.SWITCH_BUDGET_INVALID),
    ],
)
def test_validation_codes(sample_dict: dict, mutator, expected_code: str) -> None:
    data = deepcopy(sample_dict)
    mutator(data)
    with pytest.raises(BusinessError) as error:
        parse_scenario(data)
    assert error.value.code == expected_code


def test_invalid_cli_does_not_create_state(tmp_path: Path, sample_dict: dict) -> None:
    data = deepcopy(sample_dict)
    _bad_selectivity(data)
    sample = write_json(tmp_path / "bad.json", data)
    db = tmp_path / "state.db"
    result = run_cli(
        "run",
        "--sample",
        str(sample),
        "--db",
        str(db),
        "--blocks",
        str(tmp_path / "blocks"),
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == ErrorCode.BAD_STATS
    assert not db.exists()
    assert not db.with_suffix(db.suffix + ".lock").exists()


def test_duplicate_json_key_is_rejected(tmp_path: Path) -> None:
    sample = tmp_path / "duplicate.json"
    sample.write_text('{"tables":[],"tables":[]}', encoding="utf-8")
    result = run_cli(
        "run",
        "--sample",
        str(sample),
        "--db",
        str(tmp_path / "state.db"),
        "--blocks",
        str(tmp_path / "blocks"),
    )
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == ErrorCode.BAD_PLAN
    assert "duplicate key" in payload["error"]["message"]


def test_fresh_refuses_to_delete_workspace(sample_dict: dict, tmp_path: Path) -> None:
    sample = write_json(tmp_path / "sample.json", sample_dict)
    db = tmp_path / "state.db"
    result = run_cli(
        "run",
        "--fresh",
        "--sample",
        str(sample),
        "--db",
        str(db),
        "--blocks",
        ".",
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload["error"]["code"] == ErrorCode.BAD_COMMAND
    assert not db.exists()

