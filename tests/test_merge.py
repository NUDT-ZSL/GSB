from __future__ import annotations

import pytest

from ner_governance.errors import (
    E_ALIAS_UNRESOLVED,
    E_BIO_INVALID,
    E_CONFIG_INVALID,
    E_DOC_REJECTED,
    E_RESOURCE_LIMIT,
    E_TYPE_NOT_ALLOWED,
    E_TYPE_RETIRED,
    GovernanceError,
)
from ner_governance.merge import merge_annotations
from ner_governance.models import canonical_json
from ner_governance.trace import trace_label

from tests.conftest import make_config, make_type_set, records_for


def _raise(callable_):
    with pytest.raises(GovernanceError) as raised:
        callable_()
    return raised.value


def test_majority_with_lexicographic_tie_break():
    records = records_for(
        [
            ("ann_a", ["B-PER"]),
            ("ann_b", ["B-ORG"]),
            ("ann_c", ["B-PERSON"]),
            ("ann_d", ["B-ORG"]),
        ]
    )
    bundle = merge_annotations(records, make_type_set(), make_config())
    assert bundle.code == "OK"
    record = bundle.docs[0]["vote_records"][0]
    assert bundle.docs[0]["ner_tags"] == ["B-PERSON"]
    assert record["selection"]["tie_break_used"] is True
    assert record["selection"]["tie_break_annotator"] == "ann_a"
    assert record["selection"]["tied_labels"] == ["B-PERSON", "B-ORG"]


def test_annotator_priority_strategy():
    records = records_for(
        [
            ("ann_a", ["B-PER"]),
            ("ann_b", ["B-ORG"]),
            ("ann_c", ["B-PERSON"]),
            ("ann_d", ["B-GPE"]),
        ]
    )
    cfg = make_config(
        merge_strategy="annotator_priority",
        annotator_priority=["ann_d", "ann_b", "ann_a", "ann_c"],
    )
    bundle = merge_annotations(records, make_type_set(), cfg)
    record = bundle.docs[0]["vote_records"][0]
    assert bundle.docs[0]["ner_tags"] == ["B-GPE"]
    assert record["selection"]["selected_annotator_id"] == "ann_d"
    assert record["selection"]["priority_fallback_used"] is False


def test_alias_normalization_before_voting():
    records = records_for(
        [
            ("ann_a", ["B-PER"]),
            ("ann_b", ["B-PERSON_NAME"]),
            ("ann_c", ["B-PERSON"]),
        ]
    )
    bundle = merge_annotations(records, make_type_set(), make_config())
    record = bundle.docs[0]["vote_records"][0]
    assert bundle.docs[0]["ner_tags"] == ["B-PERSON"]
    assert record["choices"] == [
        {
            "label": "B-PERSON",
            "count": 3,
            "annotator_ids": ["ann_a", "ann_b", "ann_c"],
        }
    ]


def test_retired_type_substitution_returns_warning_code():
    records = records_for(
        [
            ("ann_a", ["B-LOCATION", "I-LOCATION"]),
            ("ann_b", ["B-LOC", "I-LOC"]),
            ("ann_c", ["B-GPE", "I-GPE"]),
        ]
    )
    bundle = merge_annotations(records, make_type_set(), make_config())
    assert bundle.docs[0]["ner_tags"] == ["B-GPE", "I-GPE"]
    assert bundle.code == E_TYPE_RETIRED
    assert bundle.business_codes
    assert {item["code"] for item in bundle.business_codes} == {
        E_TYPE_RETIRED
    }
    assert bundle.handling_summary["category_counts"][
        "retired_substituted"
    ] == 2
    assert bundle.docs[0]["processing_categories"] == [
        "retired_substituted"
    ]


def test_retired_type_reject():
    records = records_for([("ann_a", ["B-LOCATION"])])
    cfg = make_config(retirement_policy="reject")
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), cfg)
    )
    assert error.code == E_TYPE_RETIRED
    assert error.details["doc_id"] == "doc-1"
    assert error.details["position"] == 0
    assert error.details["label"] == "B-LOCATION"
    assert error.details["processing_category"] == "retired_rejected"


def test_retired_type_document_reject():
    records = records_for([("ann_a", ["B-LOCATION"])])
    cfg = make_config(retirement_policy="doc_reject")
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), cfg)
    )
    assert error.code == E_DOC_REJECTED
    assert error.details["doc_id"] == "doc-1"
    assert error.details["reason"] == "retired_type_doc_reject"


def test_type_not_allowed_has_context():
    records = records_for([("ann_a", ["B-UNKNOWN"])])
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), make_config())
    )
    assert error.code == E_TYPE_NOT_ALLOWED
    assert error.details == {
        "doc_id": "doc-1",
        "position": 0,
        "label": "B-UNKNOWN",
        "reason": "type_not_allowed",
        "type": "UNKNOWN",
    }


def test_disabled_alias_is_unresolved():
    records = records_for([("ann_a", ["B-OLD_ORG"])])
    error = _raise(
        lambda: merge_annotations(
            records,
            make_type_set(disabled_alias=True),
            make_config(),
        )
    )
    assert error.code == E_ALIAS_UNRESOLVED
    assert error.details["doc_id"] == "doc-1"
    assert error.details["position"] == 0
    assert error.details["label"] == "B-OLD_ORG"
    assert error.details["alias"] == "OLD_ORG"


def test_config_cannot_reactivate_disabled_type_set_alias():
    records = records_for([("ann_a", ["B-OLD_ORG"])])
    cfg = make_config(alias_mapping={"OLD_ORG": "PERSON"})
    error = _raise(
        lambda: merge_annotations(
            records,
            make_type_set(disabled_alias=True),
            cfg,
        )
    )
    assert error.code == E_CONFIG_INVALID
    assert error.details["reason"] == "disabled_alias_in_config"


def test_bio_invalid_i_without_prefix():
    records = records_for(
        [
            ("ann_a", ["O", "I-PERSON"]),
            ("ann_b", ["O", "I-PERSON"]),
        ]
    )
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), make_config())
    )
    assert error.code == E_BIO_INVALID
    assert error.details["doc_id"] == "doc-1"
    assert error.details["position"] == 1
    assert error.details["label"] == "I-PERSON"


def test_malformed_bio_label():
    records = records_for([("ann_a", ["PERSON"])])
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), make_config())
    )
    assert error.code == E_BIO_INVALID
    assert error.details["reason"] == "malformed_label"


def test_token_resource_limit():
    records = records_for([("ann_a", ["O", "O"])])
    cfg = make_config(max_tokens_per_doc=1)
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), cfg)
    )
    assert error.code == E_RESOURCE_LIMIT
    assert error.details["category"] == "token_count"
    assert error.details["actual"] == 2
    assert error.details["limit"] == 1


def test_type_count_resource_limit():
    records = records_for([("ann_a", ["O"])])
    cfg = make_config(max_types=3)
    error = _raise(
        lambda: merge_annotations(records, make_type_set(), cfg)
    )
    assert error.code == E_RESOURCE_LIMIT
    assert error.details["category"] == "type_count"


def test_resident_vote_degrade_and_trace_still_replays():
    records = records_for(
        [
            ("ann_a", ["B-PER"]),
            ("ann_b", ["B-ORG"]),
            ("ann_c", ["B-PERSON"]),
            ("ann_d", ["B-ORG"]),
        ]
    )
    cfg = make_config(
        max_resident_votes=1,
        resource_policy="degrade",
    )
    bundle = merge_annotations(records, make_type_set(), cfg)
    record = bundle.docs[0]["vote_records"][0]
    assert record["choices"] == []
    assert record["choices_omitted"] is True
    assert bundle.degradations[0]["category"] == "vote_details_omitted"
    replay = trace_label(
        bundle,
        records,
        make_type_set(),
        cfg,
        "doc-1",
        0,
    )
    assert replay["label"] == "B-PERSON"


def test_merge_is_byte_deterministic():
    records = records_for(
        [
            ("ann_a", ["B-PERSON", "O"]),
            ("ann_b", ["B-PER", "O"]),
            ("ann_c", ["B-ORG", "O"]),
        ]
    )
    first = merge_annotations(records, make_type_set(), make_config())
    second = merge_annotations(records, make_type_set(), make_config())
    assert canonical_json(first.to_dict()) == canonical_json(second.to_dict())
    assert first.digest == second.digest
