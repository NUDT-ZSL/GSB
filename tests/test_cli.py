from __future__ import annotations

import json
from pathlib import Path

from mvengine.cli import main
from mvengine.storage import initialize


def test_status_is_stable_sorted_json(
    base_package,
    tmp_path: Path,
    capsys,
) -> None:
    db = tmp_path / "state.sqlite"
    initialize(base_package, db)
    assert main(["status", "--db", str(db)]) == 0
    first = capsys.readouterr().out
    assert main(["status", "--db", str(db)]) == 0
    second = capsys.readouterr().out
    assert first == second
    value = json.loads(first)
    assert value["watermark_seq"] == 0
    assert value["migration_decision_count"] == 3
    assert [item["name"] for item in value["views"]] == sorted(
        item["name"] for item in value["views"]
    )
    keys = list(value)
    assert keys == sorted(keys)


def test_merge_cli_writes_deterministic_file(
    base_package,
    tmp_path: Path,
) -> None:
    out1 = tmp_path / "index1.json"
    out2 = tmp_path / "index2.json"
    assert main([
        "merge",
        "--v1",
        str(base_package.v1.path),
        "--v2",
        str(base_package.v2.path),
        "--out",
        str(out1),
    ]) == 0
    assert main([
        "merge",
        "--v1",
        str(base_package.v1.path),
        "--v2",
        str(base_package.v2.path),
        "--out",
        str(out2),
    ]) == 0
    assert out1.read_bytes() == out2.read_bytes()

