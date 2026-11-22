from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multipart_store.manifest import load_manifest
from multipart_store.service import MultipartStore

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sample_raw(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture
def loaded_primary(tmp_path: Path) -> tuple[MultipartStore, Path]:
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "primary.json"))
    return store, tmp_path / "store"


@pytest.fixture
def uploaded_primary(loaded_primary: tuple[MultipartStore, Path]) -> tuple[MultipartStore, Path]:
    store, data_dir = loaded_primary
    result = store.upload_round()
    assert result["succeeded_count"] == 12
    return store, data_dir


@pytest.fixture
def loaded_budget(tmp_path: Path) -> tuple[MultipartStore, Path]:
    store = MultipartStore(tmp_path / "store")
    store.load(load_manifest(SAMPLES / "budget.json"))
    return store, tmp_path / "store"
