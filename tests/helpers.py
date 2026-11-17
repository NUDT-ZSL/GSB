from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from tts_workbench.engine import RenderResult, Workbench


def render(
    input_dir: Path,
    work_dir: Path,
    *,
    extra_commands: list[Path] | None = None,
    reset: bool = False,
) -> RenderResult:
    return Workbench(input_dir=input_dir, work_dir=work_dir).run(
        extra_command_paths=extra_commands or [], reset=reset
    )


def read_outputs(work_dir: Path) -> dict[str, dict[str, np.ndarray]]:
    result: dict[str, dict[str, np.ndarray]] = {}
    output_dir = work_dir / "output"
    for wav_path in sorted(output_dir.glob("*.wav")):
        pcm, sample_rate = sf.read(str(wav_path), dtype="int16")
        assert sample_rate == 16000
        with np.load(output_dir / f"{wav_path.stem}.npz") as archive:
            arrays = {key: np.array(archive[key], copy=True) for key in archive.files}
        arrays["wav_pcm"] = np.asarray(pcm, dtype=np.int16)
        result[wav_path.stem] = arrays
    return result


def assert_outputs_equal(left: dict[str, dict[str, np.ndarray]], right: dict[str, dict[str, np.ndarray]]) -> None:
    assert sorted(left) == sorted(right)
    for utterance_id in left:
        assert sorted(left[utterance_id]) == sorted(right[utterance_id])
        for key in left[utterance_id]:
            lhs = left[utterance_id][key]
            rhs = right[utterance_id][key]
            assert lhs.dtype == rhs.dtype, (utterance_id, key, lhs.dtype, rhs.dtype)
            assert np.array_equal(lhs, rhs), (utterance_id, key)


def phoneme_report(report: dict, utterance_id: str, phoneme_index: int) -> dict:
    utterance = next(item for item in report["utterances"] if item["utterance_id"] == utterance_id)
    return next(
        item
        for item in utterance["phoneme_state_trajectories"]
        if item["phoneme_index"] == phoneme_index
    )

