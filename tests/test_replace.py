from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mvengine.errors import EngineError
from mvengine.storage import initialize, query, refresh_logs, replace_package, status


def test_full_replace_preserves_unaffected_states(
    base_package,
    next_package,
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    refresh_logs(db_path=db, logs=base_package.logs)
    before_status = status(db)
    before_query = query(db_path=db)
    before_by_name = {item["name"]: item for item in before_status["views"]}
    before_result = {item["name"]: item["result_digest"] for item in before_query["views"]}

    result = replace_package(db_path=db, package=next_package)
    assert result["classification"] == "full_replace"
    actions = {item["view_name"]: item for item in result["replacement_actions"]}
    assert actions["sales_east"]["action"] == "preserved"
    assert actions["east_high"]["action"] == "recomputed"
    assert actions["inventory_total"]["action"] == "preserved"

    after_status = status(db)
    after_query = query(db_path=db)
    after_by_name = {item["name"]: item for item in after_status["views"]}
    after_result = {item["name"]: item["result_digest"] for item in after_query["views"]}
    for name in ("sales_east", "east_summary", "east_dashboard", "inventory_total", "north_total"):
        assert after_result[name] == before_result[name]
        assert after_by_name[name]["recompute_count"] == before_by_name[name]["recompute_count"]
    assert after_by_name["sales_east"]["generation"] == 3
    assert after_by_name["east_high"]["recompute_count"] == 1
    assert replace_package(db_path=db, package=next_package)["classification"] == "no_op"


def test_append_package_applies_only_new_logs(
    base_package,
    append_package,
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    refresh_logs(db_path=db, logs=base_package.logs)
    assert replace_package(db_path=db, package=append_package)["classification"] == "append"
    result = refresh_logs(db_path=db, logs=append_package.logs)
    assert result["applied"] == [13, 14]
    assert result["skipped"] == list(range(1, 13))
    east = query(db_path=db, view_name="sales_east")["views"][0]
    assert east["metrics"] == {"count": 2, "sum": 53.0, "sum_sq": 1465.0}


def test_version_rollback_is_rejected_without_state_change(
    base_package,
    rollback_package,
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    refresh_logs(db_path=db, logs=base_package.logs)
    before = status(db)
    with pytest.raises(EngineError) as exc:
        replace_package(db_path=db, package=rollback_package)
    assert exc.value.code == "E_VERSION_ROLLBACK"
    assert status(db) == before


def test_forced_termination_during_replace_rolls_back_pointer(
    base_package,
    next_package,
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    refresh_logs(db_path=db, logs=base_package.logs)
    command = [
        sys.executable,
        "-m",
        "mvengine",
        "replace",
        "--db",
        str(db),
        "--package",
        str(sample_suite["next"]),
        "--failpoint",
        "replace_after_stage",
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 137
    assert status(db)["current_package_version"] == 1
    result = replace_package(db_path=db, package=next_package)
    assert result["classification"] == "full_replace"
    assert status(db)["current_package_version"] == 2

