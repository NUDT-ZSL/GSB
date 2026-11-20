from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from summarize_pipeline.config import parse_config
from summarize_pipeline.errors import ErrorCode, PipelineError
from summarize_pipeline.generator import (
    default_config_payload,
    generate_rows,
)
from summarize_pipeline.pipeline import (
    load_documents,
    run_all_precisions,
    run_precision,
    selection_order,
)
from summarize_pipeline.textproc import split_sentence_text, tokenize
from summarize_pipeline.utils import canonical_json, write_jsonl_atomic


def test_sentence_split_and_tokenization_are_deterministic() -> None:
    text = "Alpha one. Beta two! Gamma three?"
    assert split_sentence_text(text) == [
        "Alpha one.",
        "Beta two!",
        "Gamma three?",
    ]
    assert tokenize("Alpha, ALPHA beta-2") == ("alpha", "alpha", "beta", "2")


def test_generator_is_byte_deterministic(tmp_path: Path) -> None:
    first = generate_rows(seed=777, doc_count=3)
    second = generate_rows(seed=777, doc_count=3)
    assert first == second
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    write_jsonl_atomic(path_a, first)
    write_jsonl_atomic(path_b, second)
    assert path_a.read_bytes() == path_b.read_bytes()
    assert all(row["char_len"] == len(row["text"]) for row in first)


def test_tie_break_uses_sent_no_ascending() -> None:
    candidates = [
        {"sent_no": 4, "smoothed_score": 0.5},
        {"sent_no": 1, "smoothed_score": 0.5},
        {"sent_no": 3, "smoothed_score": 0.7},
        {"sent_no": 2, "smoothed_score": 0.5},
    ]
    assert [item["sent_no"] for item in selection_order(candidates)] == [3, 1, 2, 4]


def test_report_is_byte_deterministic(documents, config) -> None:
    first = run_all_precisions(documents, config)
    second = run_all_precisions(documents, config)
    assert first["run_hash"] == second["run_hash"]
    assert canonical_json(first) == canonical_json(second)


def test_all_precision_tiers_have_records_and_stable_small_selection(
    report,
) -> None:
    assert set(report["precisions"]) == {"float64", "float32", "fixed"}
    assert report["precisions"]["float64"]["precision_record"]["scale_factor"] is None
    assert report["precisions"]["float32"]["precision_record"]["tier"] == "float32"
    fixed_record = report["precisions"]["fixed"]["precision_record"]
    assert fixed_record["tier"] == "fixed"
    assert fixed_record["scale_factor"] == 1_000_000
    assert fixed_record["rounding"] == "nearest_even"

    reference = report["precisions"]["float64"]["documents"]
    for mode in ("float32", "fixed"):
        compare = report["precisions"][mode]["documents"]
        assert set(compare) == set(reference)
        for doc_id, doc in reference.items():
            assert (
                compare[doc_id]["selection"]["selected_sent_nos"]
                == doc["selection"]["selected_sent_nos"]
            )


def test_rewrite_is_coupled_and_denominator_is_traceable(report) -> None:
    for doc in report["precisions"]["float64"]["documents"].values():
        selection = doc["selection"]
        normalization = doc["rewrite_normalization"]
        assert normalization["denominator"]["source"]
        assert normalization["denominator"]["raw_sum"] >= 0
        assert selection["budget_chars"] == 420
        assert selection["used_chars"] <= selection["budget_chars"]
        assert doc["rewritten_text"]
        assert len(doc["rewrite_units"]) == len(
            selection["selected_sent_nos"]
        )


def test_empty_selection_is_structured_and_keeps_scores(documents, config) -> None:
    for budget in (0, 1):
        constrained = replace(config, length_budget=budget)
        result = run_precision(documents, constrained, "float64")
        assert result["documents"] == {}
        assert result["errors"]
        assert all(
            error["code"] == ErrorCode.EMPTY_SELECTION
            for error in result["errors"]
        )
        for error in result["errors"]:
            assert error["stage"] == "selection"
            assert error["doc_id"].startswith("doc-")
            assert error["detail"]["sentence_scores"]
            assert error["detail"]["normalization_denominator"]["source"] is None


def test_batch_errors_do_not_interrupt_good_documents(
    tmp_path: Path, config
) -> None:
    good = generate_rows(seed=99, doc_count=1)
    duplicate_a = dict(good[0])
    duplicate_a["doc_id"] = "doc-duplicate"
    duplicate_a["sent_no"] = 0
    duplicate_b = dict(duplicate_a)

    empty = dict(good[0])
    empty["doc_id"] = "doc-empty"
    empty["text"] = ""
    empty["char_len"] = 0

    missing = dict(good[0])
    missing["doc_id"] = "doc-missing-ref"
    missing.pop("ref_summary_units")

    path = tmp_path / "mixed.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in good + [duplicate_a, duplicate_b, empty, missing]:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    documents, errors = load_documents(path)
    assert [doc.doc_id for doc in documents] == ["doc-0001"]
    codes = {error["code"] for error in errors}
    assert {
        ErrorCode.DUPLICATE_DOC_ID,
        ErrorCode.EMPTY_TEXT,
        ErrorCode.MISSING_FIELD,
    }.issubset(codes)
    error_docs = {error["doc_id"] for error in errors}
    assert {"doc-duplicate", "doc-empty", "doc-missing-ref"}.issubset(error_docs)

    report = run_all_precisions(documents, config, input_errors=errors)
    for result in report["precisions"].values():
        assert set(result["documents"]) == {"doc-0001"}
        assert {error["code"] for error in result["errors"]}.issuperset(codes)


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("weights", "position"), 0.0, ErrorCode.INVALID_WEIGHT),
        (("smoothing",), 1.5, ErrorCode.INVALID_SMOOTHING),
        (("precision", "mode"), "float16", ErrorCode.UNKNOWN_PRECISION),
        (("precision", "mode"), "fixed", ErrorCode.INVALID_SCALE),
    ],
)
def test_config_error_codes(path, value, code) -> None:
    payload = default_config_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if path == ("precision", "mode") and value == "fixed":
        payload["precision"]["scale_factor"] = 0
    with pytest.raises(PipelineError) as raised:
        parse_config(payload)
    assert raised.value.code == code
    assert raised.value.stage == "config"
    assert raised.value.doc_id == "*"


def test_unattributed_exception_is_registered(monkeypatch, documents, config) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr("summarize_pipeline.pipeline._run_document", explode)
    result = run_precision(documents, config, "float64")
    assert result["documents"] == {}
    assert all(error["code"] == ErrorCode.UNATTRIBUTED for error in result["errors"])
    assert all(error["stage"] == "unknown" for error in result["errors"])

