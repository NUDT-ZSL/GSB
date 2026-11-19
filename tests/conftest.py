from __future__ import annotations

from pathlib import Path

import pytest

from qrw.compiler import compile_templates
from qrw.models import CompilerConfig
from qrw.readers import load_slot_types, read_queries, read_template_file


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def config() -> CompilerConfig:
    return CompilerConfig()


@pytest.fixture(scope="session")
def samples(project_root: Path) -> Path:
    return project_root / "samples"


@pytest.fixture(scope="session")
def slot_types(samples: Path):
    return load_slot_types(samples / "slot_types.json")


@pytest.fixture(scope="session")
def queries(samples: Path):
    return read_queries(samples / "queries.jsonl")


@pytest.fixture(scope="session")
def compiled_core(samples: Path, slot_types, config: CompilerConfig):
    return {
        dialect: compile_templates(
            read_template_file(samples / filename, slot_types, config),
            config,
        )
        for dialect, filename in (("v1", "v1.txt"), ("v2", "v2.txt"), ("v3", "v3.json"))
    }


@pytest.fixture(scope="session")
def compiled_advanced(samples: Path, config: CompilerConfig):
    base = samples / "advanced"
    advanced_slots = load_slot_types(base / "slot_types.json")
    return {
        dialect: compile_templates(
            read_template_file(base / filename, advanced_slots, config),
            config,
        )
        for dialect, filename in (("v1", "v1.txt"), ("v2", "v2.txt"), ("v3", "v3.json"))
    }

