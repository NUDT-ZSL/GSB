from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from tests.conftest import (
    copy_demo,
    make_tiny_input,
    update_config,
)
from tests.helpers import assert_outputs_equal, phoneme_report, read_outputs, render


def test_full_render_and_noop_reuse(tmp_path: Path) -> None:
    work = tmp_path / "work"
    first = render(Path("samples/demo"), work)
    assert first.status == "complete"
    assert first.exit_code == 0
    assert first.report["business_codes"] == []
    assert len(first.report["utterances"]) == 2
    for utterance in first.report["utterances"]:
        assert utterance["phoneme_state_trajectories"]
        for phoneme in utterance["phoneme_state_trajectories"]:
            assert [state["state"] for state in phoneme["states"]] == [
                "VOICING_DECISION",
                "F0_TRACK",
                "SPECTRAL_ENVELOPE",
                "ENERGY_NORMALIZE",
            ]
            assert phoneme["substate_position"] == "DONE"
            voicing = phoneme["states"][0]
            assert voicing["transition"]["tie_rule"] == "equality resolves to voiced"
            assert voicing["features"]["comparisons"]
            assert "score" not in voicing["features"]["comparisons"][0]

    outputs_before = read_outputs(work)
    second = render(Path("samples/demo"), work)
    assert second.status == "complete"
    assert second.report["diagnostics"]["cache"] == {
        "feature_hits": 111,
        "feature_misses": 0,
        "energy_hits": 111,
        "energy_misses": 0,
        "concat_hits": 2,
        "concat_misses": 0,
    }
    assert second.report["diagnostics"]["phonemes_reused"] == 9
    assert second.report["diagnostics"]["phonemes_recomputed"] == 0
    assert sum(
        1
        for record in second.report["command_application_records"]
        if record["status"] == "skipped_duplicate"
    ) == 4
    assert_outputs_equal(outputs_before, read_outputs(work))
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    assert state["stage"] == "complete"
    assert state["command_cursor"] == 4
    assert state["substate_positions"]["utt_alpha:0000"] == "DONE"


def test_serial_and_concurrent_outputs_match(tmp_path: Path) -> None:
    serial_input = copy_demo(tmp_path / "serial_input", with_commands=True)
    parallel_input = copy_demo(tmp_path / "parallel_input", with_commands=True)
    update_config(serial_input, {"concurrency": 1})
    update_config(parallel_input, {"concurrency": 6})

    serial = render(serial_input, tmp_path / "serial_work")
    parallel = render(parallel_input, tmp_path / "parallel_work")
    assert serial.status == parallel.status == "complete"
    assert_outputs_equal(read_outputs(tmp_path / "serial_work"), read_outputs(tmp_path / "parallel_work"))
    assert serial.report["diagnostics"]["stable_task_order"] == parallel.report[
        "diagnostics"
    ]["stable_task_order"]
    assert serial.report["diagnostics"]["energy_accumulation_order"] == parallel.report[
        "diagnostics"
    ]["energy_accumulation_order"]
    assert serial.report["diagnostics"]["cache"] == parallel.report["diagnostics"]["cache"]


def test_quantization_changes_f0_and_pcm(tmp_path: Path) -> None:
    fine_input = copy_demo(tmp_path / "fine_input", with_commands=False)
    coarse_input = copy_demo(tmp_path / "coarse_input", with_commands=False)
    update_config(fine_input, {"f0_quantization_hz": 5.0})
    update_config(coarse_input, {"f0_quantization_hz": 25.0})
    fine = render(fine_input, tmp_path / "fine_work")
    coarse = render(coarse_input, tmp_path / "coarse_work")
    assert fine.status == coarse.status == "complete"
    fine_outputs = read_outputs(tmp_path / "fine_work")
    coarse_outputs = read_outputs(tmp_path / "coarse_work")
    assert not np.array_equal(
        fine_outputs["utt_alpha"]["wav_pcm"], coarse_outputs["utt_alpha"]["wav_pcm"]
    )
    assert not np.array_equal(
        fine_outputs["utt_alpha"]["f0_hz"], coarse_outputs["utt_alpha"]["f0_hz"]
    )
    assert set(np.unique(fine_outputs["utt_alpha"]["f0_hz"])) != set(
        np.unique(coarse_outputs["utt_alpha"]["f0_hz"])
    )


def test_voicing_tie_is_deterministically_voiced(tmp_path: Path) -> None:
    sample = make_tiny_input(
        tmp_path / "tie_input",
        phones=["TIE"],
        durations=[4],
        f0_hz=[130.0],
        energy=[0.6],
        noise_floor=[0.01],
        voicing_score=[0.5],
    )
    result = render(sample, tmp_path / "tie_work")
    assert result.status == "complete"
    phoneme = phoneme_report(result.report, "utt_tiny", 0)
    comparison = phoneme["states"][0]["features"]["comparisons"][0]
    assert comparison["voicing_score"] == 0.5
    assert comparison["threshold"] == 0.5
    assert comparison["operator"] == ">="
    assert comparison["result"] is True
    assert comparison["final_voiced"] is True
    with np.load(tmp_path / "tie_work" / "output" / "utt_tiny.npz") as archive:
        assert int(np.sum(archive["voiced"])) == 4
