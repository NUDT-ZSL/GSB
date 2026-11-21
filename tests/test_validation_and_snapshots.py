from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from serverless_engine.engine import Engine
from serverless_engine.errors import EngineError
from serverless_engine.util import stable_json

from conftest import ROOT, SAMPLES, cli_json, db_rows


LOAD_ARGS = [
    "--functions",
    "samples/functions.json",
    "--layers",
    "samples/layers.json",
    "--snapshot-v1",
    "samples/snapshots/v1.json",
    "--snapshot-v2-index",
    "samples/snapshots/v2/index.json",
]


def test_sample_generator_is_deterministic(tmp_path: Path) -> None:
    outputs = []
    for index in range(2):
        target = tmp_path / f"samples{index}"
        subprocess.run(
            [
                sys.executable,
                "scripts/generate_samples.py",
                "--out",
                str(target),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(target)
    files1 = sorted(path.relative_to(outputs[0]) for path in outputs[0].rglob("*.json"))
    files2 = sorted(path.relative_to(outputs[1]) for path in outputs[1].rglob("*.json"))
    assert files1 == files2
    for relative in files1:
        assert (outputs[0] / relative).read_bytes() == (outputs[1] / relative).read_bytes()
    for index in range(2):
        connection = sqlite3.connect(outputs[index] / "snapshots" / "v2" / "v2_meta.sqlite")
        try:
            rows = connection.execute(
                "SELECT * FROM snapshot_metadata ORDER BY function"
            ).fetchall()
        finally:
            connection.close()
        assert rows == [
            ("classify-text", 22, 2, 4, "s1"),
            ("legacy-handler", 18, 2, 2, "s2"),
            ("resize-image", 20, 2, 7, "s0"),
        ]


def test_invalid_load_is_json_error_and_leaves_no_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    functions = json.loads((SAMPLES / "functions.json").read_text(encoding="utf-8"))
    functions["functions"][1]["name"] = functions["functions"][0]["name"]
    bad_functions = tmp_path / "bad-functions.json"
    bad_functions.write_text(json.dumps(functions), encoding="utf-8")
    engine = Engine(state)
    with pytest.raises(EngineError) as captured:
        engine.load(
            str(bad_functions),
            str(SAMPLES / "layers.json"),
            str(SAMPLES / "snapshots" / "v1.json"),
            str(SAMPLES / "snapshots" / "v2" / "index.json"),
        )
    assert captured.value.code == "E_BAD_FUNC"
    assert not (state / "engine.sqlite3").exists()


def test_layer_cycle_is_rejected_before_persistence(tmp_path: Path) -> None:
    state = tmp_path / "state"
    layers = json.loads((SAMPLES / "layers.json").read_text(encoding="utf-8"))
    layers["layers"][0]["depends_on"] = ["image-libs"]
    cycle_file = tmp_path / "cycle.json"
    cycle_file.write_text(json.dumps(layers), encoding="utf-8")
    with pytest.raises(EngineError) as captured:
        Engine(state).load(
            str(SAMPLES / "functions.json"),
            str(cycle_file),
            str(SAMPLES / "snapshots" / "v1.json"),
            str(SAMPLES / "snapshots" / "v2" / "index.json"),
        )
    assert captured.value.code == "E_LAYER_CYCLE"
    assert not (state / "engine.sqlite3").exists()


def test_invalid_trace_and_patch_do_not_change_state(
    loaded_engine: Engine, tmp_path: Path
) -> None:
    before = stable_json(loaded_engine.status())
    trace = json.loads((SAMPLES / "traces" / "before.json").read_text(encoding="utf-8"))
    trace["calls"][0]["occupancy"] = 99
    bad_trace = tmp_path / "bad-trace.json"
    bad_trace.write_text(json.dumps(trace), encoding="utf-8")
    with pytest.raises(EngineError) as captured:
        loaded_engine.invoke_trace(str(bad_trace))
    assert captured.value.code == "E_BAD_CALL"
    assert stable_json(loaded_engine.status()) == before

    patch = json.loads(
        (SAMPLES / "patches" / "layer-update.json").read_text(encoding="utf-8")
    )
    patch["operations"][0]["layer"] = "missing-layer"
    bad_patch = tmp_path / "bad-patch.json"
    bad_patch.write_text(json.dumps(patch), encoding="utf-8")
    with pytest.raises(EngineError) as captured:
        loaded_engine.apply_patch(str(bad_patch))
    assert captured.value.code == "E_BAD_PATCH"
    assert stable_json(loaded_engine.status()) == before


def test_snapshot_merge_migration_and_pointer(
    loaded_engine: Engine, samples: Path
) -> None:
    rows = db_rows(
        loaded_engine.store.db_path,
        "SELECT * FROM snapshot_observations ORDER BY function_name",
    )
    by_function = {row["function_name"]: row for row in rows}
    assert by_function["resize-image"]["source"] == "v2"
    assert by_function["resize-image"]["warm_generation"] == 2
    assert by_function["classify-text"]["source"] == "v2"
    assert by_function["legacy-handler"]["source"] == "v2"
    assert by_function["report-csv"]["source"] == "v1"
    assert by_function["report-csv"]["absolute_init_ms"] == 34
    assert by_function["report-csv"]["scale_num"] == 5
    assert by_function["report-csv"]["scale_den"] == 4
    assert by_function["report-csv"]["rounding"] == "half_up"

    migrations = db_rows(
        loaded_engine.store.db_path,
        "SELECT * FROM snapshot_migrations ORDER BY ordinal",
    )
    assert len(migrations) == 4
    details = [json.loads(row["detail_json"]) for row in migrations]
    assert all(item["logical_warmed_preserved"] for item in details)
    report = next(item for item in details if item["function"] == "report-csv")
    assert report["warm_hit_before"] is False
    assert report["warm_hit_after"] is False
    assert (loaded_engine.store.artifacts_dir / "merged_snapshots.json").exists()
    assert (loaded_engine.store.artifacts_dir / "snapshot_migration.json").exists()


def test_cli_business_error_shape(tmp_path: Path) -> None:
    result = cli_json(
        tmp_path / "state",
        "load",
        *LOAD_ARGS,
        expect=0,
    )
    # The sample is valid; this command must succeed, proving the default CLI paths work.
    assert result["ok"] is True

    bad = tmp_path / "missing.json"
    error = cli_json(
        tmp_path / "state2",
        "load",
        "--functions",
        str(bad),
        "--layers",
        "samples/layers.json",
        "--snapshot-v1",
        "samples/snapshots/v1.json",
        "--snapshot-v2-index",
        "samples/snapshots/v2/index.json",
        expect=2,
    )
    assert error["ok"] is False
    assert error["error"]["code"] == "E_IO"
