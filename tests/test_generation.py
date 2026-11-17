from __future__ import annotations

import hashlib
import json
from pathlib import Path

from intent_engine.data_generator import generate_dataset


def _digests(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_seeded_generation_is_byte_reproducible(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_dataset(first, seed=17)
    generate_dataset(second, seed=17)
    assert _digests(first) == _digests(second)


def test_invalid_corpus_fixture_contains_all_required_cases(data_dir):
    lines = (data_dir / "invalid_corpus.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("BAD-EMPTY" in line for line in lines)
    assert any(not line.startswith("{") for line in lines)
    assert any("BAD-INTENT" in line for line in lines)
    assert any("BAD-SPAN" in line for line in lines)

