#!/usr/bin/env python3
"""Generate a deterministic, self-contained NER governance sample."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ner_governance.config import config_from_dict  # noqa: E402
from ner_governance.merge import merge_annotations  # noqa: E402
from ner_governance.migration import render_type_set  # noqa: E402
from ner_governance.models import (  # noqa: E402
    AliasDefinition,
    TypeDefinition,
    TypeSet,
    canonical_json,
)
from ner_governance.typesets import (  # noqa: E402
    FORMAT_V1,
    FORMAT_V2,
    FORMAT_V3,
    load_type_set,
)

SEED = 20261123
LABEL_SET_VERSION = "ls-ner-3"
ANNOTATORS = ["ann_a", "ann_b", "ann_c", "ann_d"]
TOKEN_POOL = [
    "晨光",
    "研究",
    "团队",
    "在北京",
    "发布",
    "了",
    "星河",
    "公司",
    "与",
    "华东",
    "医院",
    "合作",
    "李明",
    "负责",
    "项目",
    "昨日",
    "抵达",
    "上海",
    "总部",
    "签署",
    "协议",
    "记者",
    "报道",
]
ACTIVE_TYPES = ["PERSON", "ORG", "GPE"]
ALIAS_BY_TYPE = {
    "PERSON": "PER",
    "ORG": "ORG_NAME",
    "GPE": "GPE_NAME",
}


def build_type_set() -> TypeSet:
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
            aliases=[AliasDefinition("ORG_NAME", "ORG")],
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
    return TypeSet(
        format_version="canonical",
        label_set_version=LABEL_SET_VERSION,
        types=types,
        source_name="generator",
    )


def build_config() -> dict:
    return {
        "merge_strategy": "majority",
        "annotator_priority": ["ann_b", "ann_d", "ann_a", "ann_c"],
        "alias_mapping": {"PERSON_ALIAS": "PERSON"},
        "max_tokens_per_doc": 64,
        "max_types": 8,
        "max_resident_votes": 2000,
        "retirement_policy": "substitute",
        "retirement_substitutes": {"LOCATION": "GPE"},
        "resource_policy": "fail",
        "label_set_version": LABEL_SET_VERSION,
        "downgrade_policy": {
            "nested_aliases": "flatten",
            "disabled_aliases": "discard",
            "bio_prefix_required": "flatten",
        },
    }


def _set_span(
    tags: list[str],
    start: int,
    length: int,
    type_name: str,
) -> None:
    tags[start] = f"B-{type_name}"
    for offset in range(1, length):
        tags[start + offset] = f"I-{type_name}"


def _drop_span(tags: list[str], start: int, length: int) -> None:
    for offset in range(length):
        tags[start + offset] = "O"


def build_annotations() -> list[dict]:
    rng = np.random.default_rng(SEED)
    records: list[dict] = []
    for doc_index in range(1, 9):
        doc_id = f"doc-{doc_index:03d}"
        token_count = int(rng.integers(14, 19))
        token_indexes = rng.integers(0, len(TOKEN_POOL), size=token_count)
        tokens = [TOKEN_POOL[int(index)] for index in token_indexes]
        span_types = [
            str(rng.choice(ACTIVE_TYPES)),
            "GPE",
            str(rng.choice(ACTIVE_TYPES)),
            str(rng.choice(ACTIVE_TYPES)),
        ]
        spans = [
            (1, 1, span_types[0]),
            (4, 2, span_types[1]),
            (8, 2, span_types[2]),
            (12, 2, span_types[3]),
        ]
        base = ["O"] * token_count
        for start, length, type_name in spans:
            _set_span(base, start, length, type_name)

        # Span 0 is always a 2-2 tie. Prefixes remain valid after tie-breaking.
        tie_left = "PER" if doc_index % 2 == 1 else "PERSON"
        tie_right = "ORG_NAME" if doc_index % 2 == 1 else "ORG"
        # Span 1 is retired LOCATION for two annotators and active GPE for two.
        # Span 2 uses aliases for two annotators. Span 3 drops one whole span.
        alias_type = spans[2][2]
        alias = ALIAS_BY_TYPE[alias_type]
        for annotator_index, annotator in enumerate(ANNOTATORS):
            tags = list(base)
            if annotator_index in (0, 2):
                tags[spans[0][0]] = f"B-{tie_left}"
            else:
                tags[spans[0][0]] = f"B-{tie_right}"
            if annotator_index in (0, 1):
                _set_span(
                    tags,
                    spans[1][0],
                    spans[1][1],
                    "LOCATION",
                )
            else:
                _set_span(tags, spans[1][0], spans[1][1], "GPE")
            if annotator_index in (0, 1):
                _set_span(tags, spans[2][0], spans[2][1], alias)
            if annotator_index == 0:
                _drop_span(tags, spans[3][0], spans[3][1])
            records.append(
                {
                    "doc_id": doc_id,
                    "tokens": tokens,
                    "ner_tags": tags,
                    "annotator_id": annotator,
                    "annotation_version": "ann-r1",
                    "label_set_version": LABEL_SET_VERSION,
                }
            )
    return records


def main() -> int:
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    type_set = build_type_set()
    rendered = {
        FORMAT_V1: render_type_set(type_set, FORMAT_V1),
        FORMAT_V2: render_type_set(type_set, FORMAT_V2),
        FORMAT_V3: render_type_set(type_set, FORMAT_V3),
    }
    paths = {
        FORMAT_V1: data_dir / "entity_types_v1.txt",
        FORMAT_V2: data_dir / "entity_types_v2.txt",
        FORMAT_V3: data_dir / "entity_types_v3.json",
    }
    for version, path in paths.items():
        path.write_text(rendered[version], encoding="utf-8")
    (data_dir / "annotation_config.json").write_text(
        json.dumps(
            build_config(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    annotations = build_annotations()
    (data_dir / "annotations.jsonl").write_text(
        "\n".join(canonical_json(record) for record in annotations) + "\n",
        encoding="utf-8",
    )

    loaded = {version: load_type_set(path) for version, path in paths.items()}
    fingerprints = {
        version: value.semantic_fingerprint()
        for version, value in loaded.items()
    }
    if len(set(fingerprints.values())) != 1:
        raise RuntimeError(f"generated type-set views drift: {fingerprints}")
    config = config_from_dict(build_config())
    from ner_governance.annotations import parse_annotation

    records = [
        parse_annotation(record, line=index)
        for index, record in enumerate(annotations, start=1)
    ]
    bundle = merge_annotations(records, loaded[FORMAT_V1], config)
    print(
        canonical_json(
            {
                "seed": SEED,
                "files": {
                    "annotations": str(paths[FORMAT_V1].parent / "annotations.jsonl"),
                    "annotation_config": str(
                        paths[FORMAT_V1].parent / "annotation_config.json"
                    ),
                    "type_sets": {key: str(value) for key, value in paths.items()},
                },
                "documents": len(bundle.docs),
                "annotations": len(records),
                "token_positions": sum(
                    len(doc["ner_tags"]) for doc in bundle.docs
                ),
                "type_set_fingerprint": next(iter(fingerprints.values())),
                "merge_digest": bundle.digest,
                "retirement_warnings": len(bundle.business_codes),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
