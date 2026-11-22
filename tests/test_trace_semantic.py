from __future__ import annotations

import copy

import pytest

from ner_governance.config import load_config
from ner_governance.errors import (
    E_SEMANTIC_DRIFT,
    E_TRACE_BROKEN,
    GovernanceError,
)
from ner_governance.merge import merge_annotations
from ner_governance.semantic import compare_format_views
from ner_governance.trace import trace_label, verify_all_traces
from ner_governance.typesets import load_type_set


def _sample(sample_paths):
    views = {
        name: load_type_set(sample_paths[name]) for name in ("v1", "v2", "v3")
    }
    config = load_config(sample_paths["config"])
    records = []
    from ner_governance.annotations import parse_annotation
    import json

    for line_number, line in enumerate(
        sample_paths["annotations"]
        .read_text(encoding="utf-8")
        .splitlines(),
        start=1,
    ):
        records.append(parse_annotation(json.loads(line), line=line_number))
    bundle = merge_annotations(records, views["v1"], config)
    return views, config, records, bundle


def _trace_error(callable_):
    with pytest.raises(GovernanceError) as raised:
        callable_()
    return raised.value


def test_sample_trace_and_all_replays(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    result = trace_label(
        bundle,
        records,
        views["v1"],
        config,
        "doc-001",
        1,
    )
    assert result["ok"]
    assert result["label"] == "B-PERSON"
    assert result["recomputed_choices"] == [
        {"label": "B-ORG", "count": 2, "annotator_ids": ["ann_b", "ann_d"]},
        {
            "label": "B-PERSON",
            "count": 2,
            "annotator_ids": ["ann_a", "ann_c"],
        },
    ]
    all_result = verify_all_traces(bundle, records, views["v1"], config)
    assert all_result["verified_positions"] == 134


def test_trace_detects_tampered_selected_label(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    record = bundle.docs[0]["vote_records"][1]
    record["selected_label"] = "B-ORG"
    error = _trace_error(
        lambda: trace_label(
            bundle,
            records,
            views["v1"],
            config,
            "doc-001",
            1,
        )
    )
    assert error.code == E_TRACE_BROKEN
    assert error.details["doc_id"] == "doc-001"
    assert error.details["position"] == 1
    assert error.details["reason"] == "selected_label_mismatch"


def test_trace_detects_tampered_raw_vote(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    record = bundle.docs[0]["vote_records"][1]
    record["raw_votes"][0]["raw_label"] = "B-ORG"
    error = _trace_error(
        lambda: trace_label(
            bundle,
            records,
            views["v1"],
            config,
            "doc-001",
            1,
        )
    )
    assert error.code == E_TRACE_BROKEN
    assert error.details["reason"] == "raw_vote_mismatch"
    assert error.details["doc_id"] == "doc-001"
    assert error.details["position"] == 1


def test_trace_detects_missing_vote_record(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    bundle.docs[0]["vote_records"].pop(1)
    error = _trace_error(
        lambda: trace_label(
            bundle,
            records,
            views["v1"],
            config,
            "doc-001",
            1,
        )
    )
    assert error.code == E_TRACE_BROKEN
    assert error.details["reason"] == "vote_record_missing"


def test_trace_rejects_wrong_config(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    wrong = copy.deepcopy(config)
    wrong.max_tokens_per_doc += 1
    error = _trace_error(
        lambda: trace_label(
            bundle,
            records,
            views["v1"],
            wrong,
            "doc-001",
            1,
        )
    )
    assert error.code == E_TRACE_BROKEN
    assert error.details["reason"] == "config_digest_mismatch"


def test_trace_rejects_wrong_type_set_version(sample_paths):
    views, config, records, bundle = _sample(sample_paths)
    wrong = copy.deepcopy(views["v1"])
    wrong.label_set_version = "ls-other"
    error = _trace_error(
        lambda: trace_label(
            bundle,
            records,
            wrong,
            config,
            "doc-001",
            1,
        )
    )
    assert error.code == E_TRACE_BROKEN
    assert error.details["reason"] == "label_set_version_mismatch"


def test_cross_format_success(sample_paths):
    views, config, records, _bundle = _sample(sample_paths)
    report = compare_format_views(views, records, config)
    assert report["ok"]
    assert len(set(report["fingerprints"].values())) == 1
    assert len(set(report["merge_digests"].values())) == 1
    assert report["token_position_count"] == 134


def test_semantic_drift_is_detected(sample_paths):
    views, config, records, _bundle = _sample(sample_paths)
    drifted = copy.deepcopy(views["v3"])
    drifted.type_map()["PERSON"].enabled = False
    views["v3"] = drifted
    error = _trace_error(
        lambda: compare_format_views(views, records, config)
    )
    assert error.code == E_SEMANTIC_DRIFT
    assert error.details["differing_view"] == "v3"
