from __future__ import annotations

import json
from pathlib import Path

from voice_repair.samples import generate_demo_dataset


def test_demo_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = generate_demo_dataset(tmp_path / "first", seed=20261125, force=True)
    second = generate_demo_dataset(tmp_path / "second", seed=20261125, force=True)
    first_task = json.loads(first.read_text())
    second_task = json.loads(second.read_text())
    assert first_task == second_task
    assert {item["generation"]["sample_rate"] for item in first_task["recordings"]} == {
        16000,
        44100,
        48000,
    }
    for recording in first_task["recordings"]:
        assert (tmp_path / "first" / recording["wav"]).read_bytes() == (
            tmp_path / "second" / recording["wav"]
        ).read_bytes()
