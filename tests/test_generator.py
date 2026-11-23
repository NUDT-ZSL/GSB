from __future__ import annotations

import json

from coref_pipeline.generator import generate_sample


def test_generator_seed_reproducibility(tmp_path):
    first = generate_sample(tmp_path / "first", seed=17, force=True)
    second = generate_sample(tmp_path / "second", seed=17, force=True)
    different = generate_sample(tmp_path / "different", seed=18, force=True)

    first_bytes = (tmp_path / "first" / "events.jsonl").read_bytes()
    second_bytes = (tmp_path / "second" / "events.jsonl").read_bytes()
    different_bytes = (tmp_path / "different" / "events.jsonl").read_bytes()
    assert first_bytes == second_bytes
    assert first_bytes != different_bytes
    assert first["files"] == second["files"]
    assert first["seed"] == 17
    assert different["seed"] == 18

    custom = generate_sample(
        tmp_path / "custom", seed=19, event_count=20, batch_count=2, force=True
    )
    custom_events = [
        json.loads(line)
        for line in (tmp_path / "custom" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert custom["batch_count"] == 2
    assert {event["batch_no"] for event in custom_events} <= {1, 2}
