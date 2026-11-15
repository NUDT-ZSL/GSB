from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tactical_engine.sample_data import generate_sample
from tactical_engine.scenario import load_scenario, load_scenario_parts


def test_cli_run_and_resume(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scenario = root / "samples" / "sample_scenario.json"
    output = tmp_path / "output"
    state = tmp_path / "state"
    first = subprocess.run(
        [
            sys.executable,
            "-m",
            "tactical_engine",
            "run",
            "--scenario",
            str(scenario),
            "--output",
            str(output),
            "--state",
            str(state),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert (output / "replay.json").exists()
    snapshot = state / "snapshot_002.json"
    resumed_output = tmp_path / "output_resume"
    resumed_state = tmp_path / "state_resume"
    second = subprocess.run(
        [
            sys.executable,
            "-m",
            "tactical_engine",
            "run",
            "--scenario",
            str(scenario),
            "--output",
            str(resumed_output),
            "--state",
            str(resumed_state),
            "--resume",
            str(snapshot),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    full = json.loads((output / "replay.json").read_text(encoding="utf-8"))
    resumed = json.loads((resumed_output / "replay.json").read_text(encoding="utf-8"))
    assert resumed["resume"]["accepted"] is True
    assert resumed["decisions"] == full["decisions"]
    assert resumed["final_units"] == full["final_units"]
    assert resumed["digest"] == full["digest"]


def test_split_inputs_match_combined_scene(tmp_path):
    paths = generate_sample(tmp_path / "samples")
    combined = load_scenario(paths["scenario"])
    split = load_scenario_parts(
        paths["terrain"],
        paths["units"],
        paths["replay"],
        paths["options"],
    )
    assert split.fingerprint == combined.fingerprint
    assert split.units == combined.units
    assert split.replay == combined.replay


def test_cli_invalid_json_returns_business_error(tmp_path):
    root = Path(__file__).resolve().parents[1]
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tactical_engine",
            "run",
            "--scenario",
            str(invalid),
            "--output",
            str(tmp_path / "output"),
            "--state",
            str(tmp_path / "state"),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "E_JSON_INVALID"


def test_gui_self_check_matches_cli(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from tactical_engine.gui import TacticalWindow
    from tactical_engine.pipeline import run_scenario_files

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "output"
    state = tmp_path / "state"
    run_scenario_files(root / "samples" / "sample_scenario.json", output, state)
    application = QApplication.instance() or QApplication([])
    window = TacticalWindow(
        root / "samples" / "sample_scenario.json",
        output / "summary.json",
    )
    assert window.execution is not None
    for _ in range(3):
        window._step()
    window.unit_combo.setCurrentIndex(1)
    window.threshold_edit.setText("1e-6")
    window._locate_first_exceed()
    window.close()
    check = json.loads((output / "ui_selfcheck.json").read_text(encoding="utf-8"))
    assert check["matches_cli"] is True
    assert check["interactive_integrity"] is True
    assert check["ok"] is True
