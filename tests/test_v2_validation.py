from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from mvengine.definitions import parse_v2
from mvengine.errors import EngineError
from mvengine.package import load_package


def _v2_copy(sample_suite: dict[str, Path], target: Path) -> Path:
    source = load_package(sample_suite["base"]).v2.path.parent
    shutil.copytree(source, target)
    return target


def test_generation_history_must_be_strict(
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = _v2_copy(sample_suite, tmp_path / "v2")
    connection = sqlite3.connect(root / "metadata.sqlite")
    try:
        connection.execute(
            "UPDATE view_revisions SET generation=0 WHERE view_name='sales_east' AND generation=2"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(EngineError) as exc:
        parse_v2(root)
    assert exc.value.code == "E_GENERATION_NOT_MONOTONIC"


def test_tombstone_requires_reason(
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = _v2_copy(sample_suite, tmp_path / "v2")
    connection = sqlite3.connect(root / "metadata.sqlite")
    try:
        connection.execute(
            "UPDATE view_revisions SET tombstone_reason=NULL "
            "WHERE view_name='legacy_west' AND status='tombstoned'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(EngineError) as exc:
        parse_v2(root)
    assert exc.value.code == "E_TOMBSTONE_INCONSISTENT"


def test_absolute_refresh_time_must_be_integer_ms(
    sample_suite: dict[str, Path],
    tmp_path: Path,
) -> None:
    root = _v2_copy(sample_suite, tmp_path / "v2")
    connection = sqlite3.connect(root / "metadata.sqlite")
    try:
        connection.execute(
            "UPDATE view_revisions SET absolute_refresh_ms=100000.5 "
            "WHERE view_name='sales_east' AND generation=2"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(EngineError) as exc:
        parse_v2(root)
    assert exc.value.code == "E_BAD_REFRESH_TIME"

