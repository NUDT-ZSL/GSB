from __future__ import annotations

from pathlib import Path

import pytest

from summarize_pipeline.config import parse_config
from summarize_pipeline.generator import default_config_payload, generate_rows
from summarize_pipeline.pipeline import load_documents, run_all_precisions
from summarize_pipeline.utils import write_jsonl_atomic


@pytest.fixture()
def config():
    return parse_config(default_config_payload())


@pytest.fixture()
def sample_path(tmp_path: Path) -> Path:
    path = tmp_path / "samples.jsonl"
    write_jsonl_atomic(path, generate_rows(seed=42, doc_count=4))
    return path


@pytest.fixture()
def documents_and_errors(sample_path: Path):
    return load_documents(sample_path)


@pytest.fixture()
def documents(documents_and_errors):
    return documents_and_errors[0]


@pytest.fixture()
def report(documents, config):
    return run_all_precisions(documents, config)

