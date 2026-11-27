from __future__ import annotations

from georope.generator import deterministic_json, generate_sample


def test_generator_is_deterministic() -> None:
    first = deterministic_json(generate_sample(42))
    second = deterministic_json(generate_sample(42))
    assert first == second


def test_different_seed_changes_features() -> None:
    first = deterministic_json(generate_sample(42))
    second = deterministic_json(generate_sample(43))
    assert first != second
