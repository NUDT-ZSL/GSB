from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctr_pipeline.canonical import atomic_write_bytes, canonical_json_bytes
from ctr_pipeline.errors import E_CONFIG_INVALID, E_DUP_SAMPLE, PipelineError
from ctr_pipeline.generator import generate_dataset
from ctr_pipeline.ingestion import (
    canonical_expanded_bytes,
    expand_dataset,
    load_feature_dictionary,
    load_source_samples,
)


def _rows(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_rows(path: Path, rows: list[dict]) -> None:
    atomic_write_bytes(
        path,
        b"".join(canonical_json_bytes(row) + b"\n" for row in rows),
    )


def test_same_seed_produces_byte_identical_files(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest_a = generate_dataset(first, seed=17, sample_count=20)
    manifest_b = generate_dataset(second, seed=17, sample_count=20)
    assert manifest_a == manifest_b
    assert (first / "exposures.jsonl").read_bytes() == (
        second / "exposures.jsonl"
    ).read_bytes()
    assert (first / "features.jsonl").read_bytes() == (
        second / "features.jsonl"
    ).read_bytes()


def test_dictionary_and_exposure_order_do_not_change_expansion(
    generated_dataset: Path,
    tmp_path: Path,
) -> None:
    dictionary = load_feature_dictionary(generated_dataset / "features.jsonl")
    sources = load_source_samples(generated_dataset / "exposures.jsonl", dictionary)
    baseline = expand_dataset(sources, dictionary, feature_dimension=128)

    shuffled_dictionary = tmp_path / "features-shuffled.jsonl"
    _write_rows(shuffled_dictionary, list(reversed(_rows(generated_dataset / "features.jsonl"))))
    shuffled_samples = tmp_path / "exposures-shuffled.jsonl"
    _write_rows(shuffled_samples, list(reversed(_rows(generated_dataset / "exposures.jsonl"))))

    dictionary_2 = load_feature_dictionary(shuffled_dictionary)
    sources_2 = load_source_samples(shuffled_samples, dictionary_2)
    expanded_2 = expand_dataset(sources_2, dictionary_2, feature_dimension=128)
    assert baseline.checksum == expanded_2.checksum
    assert canonical_expanded_bytes(baseline.samples) == canonical_expanded_bytes(
        expanded_2.samples
    )
    assert [sample.sample_id for sample in baseline.samples] == sorted(
        sample.sample_id for sample in sources
    )
    for sample in baseline.samples:
        assert list(sample.indices) == sorted(sample.indices)
        assert len(set(sample.indices)) == len(sample.indices)
        assert all(0 <= index < 128 for index in sample.indices)


def test_duplicate_sample_returns_structured_error(
    generated_dataset: Path,
    tmp_path: Path,
) -> None:
    rows = _rows(generated_dataset / "exposures.jsonl")
    duplicate = tmp_path / "duplicate.jsonl"
    _write_rows(duplicate, rows + [rows[0]])
    dictionary = load_feature_dictionary(generated_dataset / "features.jsonl")
    with pytest.raises(PipelineError) as caught:
        load_source_samples(duplicate, dictionary)
    error = caught.value
    assert error.code == E_DUP_SAMPLE
    assert error.details["field"] == "sample_id"
    assert error.details["sample_id"] == rows[0]["sample_id"]
    assert error.details["duplicate_line_number"] == len(rows) + 1


def test_non_binary_label_returns_structured_error(
    generated_dataset: Path,
    tmp_path: Path,
) -> None:
    rows = _rows(generated_dataset / "exposures.jsonl")
    rows[0]["label"] = 2
    invalid = tmp_path / "invalid-label.jsonl"
    _write_rows(invalid, rows)
    dictionary = load_feature_dictionary(generated_dataset / "features.jsonl")
    with pytest.raises(PipelineError) as caught:
        load_source_samples(invalid, dictionary)
    error = caught.value
    assert error.code == E_CONFIG_INVALID
    assert error.details["field"] == "label"


def test_non_positive_dictionary_dimension_is_rejected(
    generated_dataset: Path,
    tmp_path: Path,
) -> None:
    rows = _rows(generated_dataset / "features.jsonl")
    rows[0]["dimension"] = 0
    invalid = tmp_path / "invalid-dimension.jsonl"
    _write_rows(invalid, rows)
    with pytest.raises(PipelineError) as caught:
        load_feature_dictionary(invalid)
    error = caught.value
    assert error.code == E_CONFIG_INVALID
    assert error.details["field"] == "dimension"
    assert error.details["feature_index"] == rows[0]["feature_index"]
