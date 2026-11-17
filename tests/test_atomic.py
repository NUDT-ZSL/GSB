from __future__ import annotations

import json
from pathlib import Path

import authdiag.util as util


def test_atomic_write_validates_and_cleans_temp_on_replace_failure(
    tmp_path: Path,
    monkeypatch,
):
    target = tmp_path / "snapshot.json"
    original = b'{"old":true}\n'
    target.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(util.os, "replace", fail_replace)
    try:
        util.atomic_write_json(target, {"new": True})
    except OSError:
        pass
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_json_is_parseable(tmp_path: Path):
    target = tmp_path / "value.json"
    util.atomic_write_json(target, {"b": 2, "a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": 2}
    assert not list(tmp_path.glob(".*.tmp"))
