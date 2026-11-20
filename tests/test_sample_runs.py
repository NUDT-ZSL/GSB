from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, SAMPLES, json_stdout


def _db(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "workspace" / "colagg.sqlite3")
    conn.row_factory = sqlite3.Row
    return conn


def test_sample_generator_is_byte_deterministic(tmp_path: Path) -> None:
    out = tmp_path / "samples"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_samples.py"), "--out", str(out)],
        cwd=ROOT,
        check=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    generated = sorted(path.relative_to(out) for path in out.rglob("*.json"))
    committed = sorted(path.relative_to(SAMPLES) for path in SAMPLES.rglob("*.json"))
    assert generated == committed
    for relative in committed:
        assert (out / relative).read_bytes() == (SAMPLES / relative).read_bytes()


def test_fixed_sample_spills_reads_back_and_verifies(clean_fixed_run: dict) -> None:
    executed = clean_fixed_run["execute"]
    assert executed.returncode == 0, executed.stderr
    status = json_stdout(clean_fixed_run["status"])
    verify = json_stdout(clean_fixed_run["verify"])
    assert clean_fixed_run["verify"].returncode == 0

    assert status["mode"] == "fixed"
    assert status["progress"] == {
        "loaded": 12,
        "pending": 0,
        "committed": 12,
        "failed": 0,
        "results": 1927,
    }
    assert len(status["groups"]) == 200
    assert status["spill"]["totals"]["spill"]["rounds"] > 0
    assert status["spill"]["totals"]["readback"]["rounds"] > 0
    assert status["peak_resident_bytes"] <= status["plan"]["memory_budget_bytes"]
    assert status["conversion_audit"]["conversions"] == 9600
    assert status["conversion_audit"]["nan"] == 4
    assert status["conversion_audit"]["infinities"] == 5
    assert status["conversion_audit"]["rounded_up"] > 0
    assert status["conversion_audit"]["rounded_down"] > 0

    assert verify["ok"] is True
    assert verify["state_digest_matches"] is True
    assert verify["windows"]["expected_windows"] == 927
    assert verify["windows"]["actual_windows"] == 927
    assert verify["windows"]["divergences"] == []
    assert verify["aggregates"]["aggregates"] == 1000
    assert verify["aggregates"]["mismatches"] == []

    root = clean_fixed_run["root"]
    spill_rows = _db(root).execute("SELECT * FROM spill_files").fetchall()
    assert spill_rows
    for row in spill_rows:
        path = root / row["rel_path"]
        assert path.exists()
        assert path.parent == (root / "spill").resolve()
    assert not list((root / "spill").glob(".tmp-*"))


def test_fixed_last_touch_and_status_sorting_are_stable(clean_fixed_run: dict) -> None:
    status = json_stdout(clean_fixed_run["status"])
    groups = status["groups"]
    assert groups == sorted(groups, key=lambda item: (str(item["group"]), item["group_hash"]))
    assert all(group["version"] == 12 for group in groups)


def test_two_clean_fixed_runs_are_byte_identical(
    clean_fixed_run: dict,
    clean_fixed_run_repeat: dict,
) -> None:
    assert clean_fixed_run["load"].stdout == clean_fixed_run_repeat["load"].stdout
    assert clean_fixed_run["execute"].stdout == clean_fixed_run_repeat["execute"].stdout
    assert clean_fixed_run["status"].stdout == clean_fixed_run_repeat["status"].stdout
    assert clean_fixed_run["verify"].stdout == clean_fixed_run_repeat["verify"].stdout
    first = json_stdout(clean_fixed_run["status"])
    second = json_stdout(clean_fixed_run_repeat["status"])
    assert first["state_digest"] == second["state_digest"]
    assert first["clock"] == second["clock"]
    assert first["spill"] == second["spill"]


def test_float_path_verifies_with_same_window_keys(clean_float_run: dict) -> None:
    assert clean_float_run["execute"].returncode == 0
    verify = json_stdout(clean_float_run["verify"])
    status = json_stdout(clean_float_run["status"])
    assert clean_float_run["verify"].returncode == 0
    assert status["mode"] == "float"
    assert verify["windows"]["actual_windows"] == 927
    assert verify["windows"]["divergences"] == []
    assert status["conversion_audit"]["nan"] == 4
    assert status["conversion_audit"]["infinities"] == 5
    with _db(clean_float_run["root"]) as conn:
        sample = conn.execute(
            "SELECT raw_repr, scale, direction FROM conversion_audit LIMIT 1"
        ).fetchone()
    assert sample["scale"] == 0
    assert sample["direction"] == "float64"


def test_two_clean_float_runs_are_byte_identical(
    clean_float_run: dict,
    clean_float_run_repeat: dict,
) -> None:
    assert clean_float_run["load"].stdout == clean_float_run_repeat["load"].stdout
    assert clean_float_run["execute"].stdout == clean_float_run_repeat["execute"].stdout
    assert clean_float_run["status"].stdout == clean_float_run_repeat["status"].stdout
    assert clean_float_run["verify"].stdout == clean_float_run_repeat["verify"].stdout
    assert (
        json_stdout(clean_float_run["status"])["state_digest"]
        == json_stdout(clean_float_run_repeat["status"])["state_digest"]
    )


def test_conversion_audit_preserves_each_fixed_raw_value(clean_fixed_run: dict) -> None:
    with _db(clean_fixed_run["root"]) as conn:
        rows = conn.execute(
            """
            SELECT column_name, raw_repr, scale, direction, scaled, nonfinite
            FROM conversion_audit
            WHERE batch_seq = 1 AND row_index = 0
            ORDER BY column_name
            """
        ).fetchall()
    assert len(rows) == 2
    assert [row["column_name"] for row in rows] == ["units", "value"]
    assert all(row["scale"] == 1000 for row in rows)
    assert all(float.fromhex(row["raw_repr"]) is not None for row in rows)
