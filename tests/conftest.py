from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from ner_governance.annotations import parse_annotation
from ner_governance.config import GovernanceConfig, config_from_dict
from ner_governance.models import (
    AliasDefinition,
    AnnotationRecord,
    TypeDefinition,
    TypeSet,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def config_dict(**overrides: Any) -> dict[str, Any]:
    value = {
        "merge_strategy": "majority",
        "annotator_priority": ["ann_a", "ann_b", "ann_c", "ann_d"],
        "alias_mapping": {},
        "max_tokens_per_doc": 32,
        "max_types": 8,
        "max_resident_votes": 1000,
        "retirement_policy": "substitute",
        "retirement_substitutes": {"LOCATION": "GPE"},
        "resource_policy": "fail",
        "label_set_version": "ls-test",
        "downgrade_policy": {
            "nested_aliases": "flatten",
            "disabled_aliases": "discard",
            "bio_prefix_required": "flatten",
        },
    }
    value.update(overrides)
    return value


def make_config(**overrides: Any) -> GovernanceConfig:
    return config_from_dict(config_dict(**overrides))


def make_type_set(
    *,
    disabled_alias: bool = False,
    extra_types: list[TypeDefinition] | None = None,
) -> TypeSet:
    types = [
        TypeDefinition(
            name="GPE",
            bio_prefix_required=True,
            enabled=True,
            aliases=[AliasDefinition("GPE_NAME", "GPE")],
        ),
        TypeDefinition(
            name="LOCATION",
            bio_prefix_required=True,
            enabled=False,
            aliases=[AliasDefinition("LOC", "LOCATION")],
        ),
        TypeDefinition(
            name="ORG",
            bio_prefix_required=True,
            enabled=True,
            aliases=[
                AliasDefinition(
                    "OLD_ORG",
                    "ORG",
                    enabled=not disabled_alias,
                )
            ],
        ),
        TypeDefinition(
            name="PERSON",
            bio_prefix_required=True,
            enabled=True,
            aliases=[
                AliasDefinition("PER", "PERSON"),
                AliasDefinition("PERSON_NAME", "PERSON"),
            ],
        ),
    ]
    if extra_types:
        types.extend(copy.deepcopy(extra_types))
    return TypeSet(
        format_version="canonical",
        label_set_version="ls-test",
        types=types,
    )


def records_for(
    votes: list[tuple[str, list[str]]],
    *,
    doc_id: str = "doc-1",
    tokens: list[str] | None = None,
) -> list[AnnotationRecord]:
    if tokens is None:
        tokens = [f"t{index}" for index in range(len(votes[0][1]))]
    raw_records = []
    for annotator, tags in votes:
        raw_records.append(
            parse_annotation(
                {
                    "doc_id": doc_id,
                    "tokens": tokens,
                    "ner_tags": tags,
                    "annotator_id": annotator,
                    "annotation_version": "v1",
                    "label_set_version": "ls-test",
                },
                line=len(raw_records) + 1,
            )
        )
    return raw_records


@pytest.fixture(scope="session")
def sample_paths() -> dict[str, Path]:
    return {
        "v1": DATA / "entity_types_v1.txt",
        "v2": DATA / "entity_types_v2.txt",
        "v3": DATA / "entity_types_v3.json",
        "config": DATA / "annotation_config.json",
        "annotations": DATA / "annotations.jsonl",
    }
