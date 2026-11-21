from __future__ import annotations

from pathlib import Path

from mvengine.package import load_package
from mvengine.samplegen import generate_suite
from mvengine.util import digest_json


def test_sample_generation_is_deterministic(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_paths = generate_suite(first_root)
    second_paths = generate_suite(second_root)
    first = load_package(Path(first_paths["base"]))
    second = load_package(Path(second_paths["base"]))
    assert first.index.index_digest == second.index.index_digest
    assert first.definition_digest == second.definition_digest
    assert first.logs.log_digest == second.logs.log_digest
    first_contract = {
        "definition_digest": first.definition_digest,
        "file_hashes": first.file_hashes,
        "index_digest": first.index.index_digest,
        "log_digest": first.log_digest,
        "migrations": first.index.migrations,
        "views": first.index.payload["views"],
    }
    second_contract = {
        "definition_digest": second.definition_digest,
        "file_hashes": second.file_hashes,
        "index_digest": second.index.index_digest,
        "log_digest": second.log_digest,
        "migrations": second.index.migrations,
        "views": second.index.payload["views"],
    }
    assert digest_json(first_contract) == digest_json(second_contract)

