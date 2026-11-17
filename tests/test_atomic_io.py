from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from supply_diag.report import atomic_write_json


def test_atomic_write_preserves_old_file_if_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "report.json"
    destination.write_text('{"old": true}\n', encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_json(destination, {"new": True})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_replaces_complete_json(tmp_path: Path) -> None:
    destination = tmp_path / "nested/report.json"
    atomic_write_json(destination, {"b": 2, "a": 1})
    assert json.loads(destination.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert not list(destination.parent.glob(".*.tmp"))

