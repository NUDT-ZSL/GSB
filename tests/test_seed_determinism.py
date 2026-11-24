from __future__ import annotations

import hashlib

from formbench.generator import generate_dataset


def test_seed_generation_is_byte_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_dataset(first)
    generate_dataset(second)

    first_files = {
        path.relative_to(first).as_posix(): path
        for path in (first / "samples").rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second).as_posix(): path
        for path in (second / "samples").rglob("*")
        if path.is_file()
    }
    assert set(first_files) == set(second_files)
    for relative, first_path in first_files.items():
        first_hash = hashlib.sha256(first_path.read_bytes()).hexdigest()
        second_hash = hashlib.sha256(second_files[relative].read_bytes()).hexdigest()
        assert first_hash == second_hash, relative
