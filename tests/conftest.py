import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from piano_workbench.controller import Controller  # noqa: E402
from piano_workbench.synth import synthesize_piano, write_wav  # noqa: E402


@pytest.fixture()
def piano_wav(tmp_path):
    pcm = synthesize_piano(duration_seconds=2.0, midi_note=60)
    path = write_wav(tmp_path / "piano.wav", pcm)
    return path, pcm


@pytest.fixture()
def controller(piano_wav):
    path, _ = piano_wav
    return Controller.from_wav(path, path.parent / "ws")
