from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from asr_editor.samples import generate_samples


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destination = tmp_path_factory.mktemp("samples")
    generate_samples(destination)
    return destination


@pytest.fixture
def sample01_files(tmp_path: Path, sample_dir: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for source in sample_dir.glob("sample01.*"):
        target = tmp_path / source.name
        shutil.copy2(source, target)
        result[source.suffix] = target
    return {
        "v1": tmp_path / "sample01.v1.json",
        "v2": tmp_path / "sample01.v2.srt",
        "sidecar": tmp_path / "sample01.tokens.meta.json",
        "wav": tmp_path / "sample01.wav",
        "dir": tmp_path,
    }
