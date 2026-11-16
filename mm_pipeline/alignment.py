"""Evidence relation alignment and dangling-relation handling."""
from __future__ import annotations

from .config import PipelineConfig
from .features import FORCE_FAILURE
from .sample import Sample

E_RELATION_DANGLING = "E_RELATION_DANGLING"


class AlignmentError(RuntimeError):
    """Raised when relation alignment fails for a sample."""


def align_relations(sample: Sample, relations: list[dict],
                    visual: dict, text: dict, cfg: PipelineConfig):
    """Align text assertions with visual regions following source order."""
    if FORCE_FAILURE.get(sample.name) == "relation_alignment":
        raise AlignmentError(f"forced alignment failure for {sample.name}")

    region_by_id = {item["region_id"]: item for item in visual["regions"]}
    assertion_by_id = {item["assertion_id"]: item
                       for item in text["assertions"]}

    aligned = []
    dangling = []
    for source_order, relation in enumerate(relations):
        missing_region = relation["region_id"] not in region_by_id
        missing_assertion = relation["assertion_id"] not in assertion_by_id
        if missing_region or missing_assertion:
            dangling.append({
                "sample_name": sample.name,
                "round": None,
                "stage": "relation_alignment",
                "code": E_RELATION_DANGLING,
                "relation_id": relation["relation_id"],
                "assertion_id": relation["assertion_id"],
                "region_id": relation["region_id"],
                "missing": [*(["region_id"] if missing_region else []),
                            *(["assertion_id"] if missing_assertion else [])],
            })
            continue
        aligned.append({
            "relation_id": relation["relation_id"],
            "source_order": source_order,
            "type": relation["type"],
            "weight": float(relation["weight"]),
            "assertion_id": relation["assertion_id"],
            "region_id": relation["region_id"],
            "assertion": assertion_by_id[relation["assertion_id"]],
            "region": region_by_id[relation["region_id"]],
        })
    return aligned, dangling
