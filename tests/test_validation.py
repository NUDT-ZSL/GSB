from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf

from tests.conftest import copy_demo, update_config
from tests.helpers import assert_outputs_equal, read_outputs, render


def test_audio_label_and_partial_failure_codes(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "invalid_input", with_commands=False)
    sf.write(
        str(sample / "bad_rate.wav"),
        np.zeros(4000, dtype=np.int16),
        8000,
        subtype="PCM_16",
        format="WAV",
    )
    shutil.copyfile(sample / "utt_alpha.wav", sample / "orphan.wav")
    with (sample / "labels.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "utterance_id": "utt_bad_audio",
                    "wav": "bad_rate.wav",
                    "phonemes": ["A"],
                    "duration_frames": [4],
                    "f0_hz": [120.0],
                    "energy": [0.5],
                    "noise_floor": [0.01],
                    "voicing_score": [0.8],
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "utterance_id": "utt_bad_label",
                    "wav": "utt_alpha.wav",
                    "phonemes": ["A", "B"],
                    "duration_frames": [4],
                }
            )
            + "\n"
        )
        for _ in range(2):
            handle.write(
                json.dumps(
                    {
                        "utterance_id": "utt_duplicate",
                        "wav": "utt_alpha.wav",
                        "phonemes": ["A"],
                        "duration_frames": [4],
                        "f0_hz": [120.0],
                        "energy": [0.5],
                        "noise_floor": [0.01],
                        "voicing_score": [0.8],
                    }
                )
                + "\n"
            )
        handle.write("{not valid json}\n")

    result = render(sample, tmp_path / "invalid_work")
    assert result.status == "complete"
    assert result.exit_code == 0
    codes = {failure["code"] for failure in result.report["failures"]}
    assert codes == {"E_AUDIO_FORMAT", "E_LABEL_INVALID"}
    assert result.report["business_codes"] == ["E_AUDIO_FORMAT", "E_LABEL_INVALID"]
    outputs = read_outputs(tmp_path / "invalid_work")
    assert sorted(outputs) == ["utt_alpha", "utt_beta"]
    failure_order = result.report["diagnostics"]["failure_order"]
    assert failure_order == sorted(
        failure_order,
        key=lambda item: (
            item["scope"],
            item["path"] or "",
            item["line"] if item["line"] is not None else -1,
            item["utterance_id"] or "",
            item["code"],
        ),
    )


def test_invalid_config_never_silently_downgrades(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "bad_config_input", with_commands=False)
    update_config(sample, {"concurrency": 0})
    result = render(sample, tmp_path / "bad_config_work")
    assert result.status == "failed"
    assert result.exit_code == 2
    assert result.report["failures"][0]["code"] == "E_CONFIG_INVALID"
    assert not (tmp_path / "bad_config_work" / "output").exists()

    second = copy_demo(tmp_path / "bad_frames_input", with_commands=False)
    update_config(second, {"frame_length_ms": 25.0, "frame_shift_ms": 10.0})
    result = render(second, tmp_path / "bad_frames_work")
    assert result.report["failures"][0]["code"] == "E_CONFIG_INVALID"


def test_config_hash_mismatch_rejects_resume_and_preserves_outputs(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "mismatch_input", with_commands=False)
    work = tmp_path / "mismatch_work"
    first = render(sample, work)
    assert first.status == "complete"
    before_outputs = read_outputs(work)
    before_state = (work / "state.json").read_bytes()

    update_config(sample, {"f0_quantization_hz": 11.0})
    rejected = render(sample, work)
    assert rejected.status == "rejected"
    assert rejected.exit_code == 78
    assert rejected.report["business_codes"] == ["E_CONFIG_MISMATCH"]
    assert (work / "state.json").read_bytes() == before_state
    assert_outputs_equal(before_outputs, read_outputs(work))


def test_snapshot_version_mismatch_rejects_resume(tmp_path: Path) -> None:
    sample = copy_demo(tmp_path / "version_input", with_commands=False)
    work = tmp_path / "version_work"
    assert render(sample, work).status == "complete"
    snapshot = json.loads((work / "state.json").read_text(encoding="utf-8"))
    snapshot["snapshot_schema_version"] = 999
    (work / "state.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    rejected = render(sample, work)
    assert rejected.status == "rejected"
    assert rejected.exit_code == 78
    assert rejected.report["business_codes"] == ["E_CONFIG_MISMATCH"]
