"""Text assertion features and evidence-relation alignment."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from .errors import E_RELATION_DANGLING, PipelineError, STAGE_ALIGN

RELATION_ASSOCIATED = "associated"
RELATION_CONFLICT = "conflict"
RELATION_SUPPLEMENT = "supplement"
RELATION_KINDS = (RELATION_ASSOCIATED, RELATION_CONFLICT, RELATION_SUPPLEMENT)

_ATTRIBUTES = ("brightness", "symmetry", "texture", "shape", "color")


@dataclass(frozen=True)
class Relation:
    relation_id: str
    kind: str
    assertion_id: str
    region_id: str
    weight: float

    def to_dict(self) -> dict:
        return {
            "relation_id": self.relation_id,
            "kind": self.kind,
            "assertion_id": self.assertion_id,
            "region_id": self.region_id,
            "weight": self.weight,
        }


def _assertion_vector(assertion: dict, feature_dim: int) -> np.ndarray:
    """Map one assertion to a deterministic L2-normalized attribute vector.

    The vector derives solely from the assertion id, attribute and value via
    a seeded NumPy generator: no global RNG state is consumed, so concurrent
    workers always derive identical vectors for identical assertions.
    """
    digest = hashlib.sha256(
        f"{assertion['assertion_id']}|{assertion['attribute']}"
        .encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "little") & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=feature_dim)
    if assertion["attribute"] in _ATTRIBUTES:
        vector[_ATTRIBUTES.index(assertion["attribute"])] += 1.0
    vector += float(assertion["value"])
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-12:
        return np.zeros(feature_dim, dtype=np.float64)
    return vector / norm


def text_features(assertions: dict, feature_dim: int) -> dict[str, np.ndarray]:
    """Return ``assertion_id -> feature vector`` for every assertion."""
    return {
        assertion["assertion_id"]: _assertion_vector(assertion, feature_dim)
        for assertion in assertions["assertions"]
    }


def load_relation_table(path: str) -> list[Relation]:
    """Parse and structurally validate the evidence relation table."""
    import json

    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or "relations" not in document:
        raise ValueError("relation table must be an object with 'relations'")
    relations: list[Relation] = []
    seen = set()
    for entry in document["relations"]:
        for field in ("relation_id", "kind", "assertion_id", "region_id"):
            if field not in entry:
                raise ValueError(f"relation entry missing field: {field}")
        if entry["kind"] not in RELATION_KINDS:
            raise ValueError(f"unknown relation kind: {entry['kind']}")
        if entry["relation_id"] in seen:
            raise ValueError(f"duplicate relation_id: {entry['relation_id']}")
        seen.add(entry["relation_id"])
        weight = float(entry.get("weight", 1.0))
        if not np.isfinite(weight) or weight <= 0:
            raise ValueError("relation weight must be a positive finite number")
        relations.append(Relation(
            relation_id=entry["relation_id"],
            kind=entry["kind"],
            assertion_id=entry["assertion_id"],
            region_id=entry["region_id"],
            weight=weight,
        ))
    return relations


def region_index(region_id: str) -> int:
    """Parse ``region-k`` identifiers used throughout the pipeline."""
    prefix = "region-"
    if not region_id.startswith(prefix):
        raise ValueError(f"malformed region id: {region_id}")
    return int(region_id[len(prefix):])


def validate_relations(relations: list[Relation],
                       assertion_ids: set[str],
                       region_count: int) -> list[dict]:
    """Return dangling relation records; valid relations are returned too.

    A relation is dangling when it references an unknown assertion or a
    region index outside ``[0, region_count)``. Dangling entries are reported,
    never silently treated as associations.
    """
    dangling: list[dict] = []
    for relation in relations:
        reasons = []
        if relation.assertion_id not in assertion_ids:
            reasons.append("unknown_assertion")
        index = None
        try:
            index = region_index(relation.region_id)
            if not 0 <= index < region_count:
                reasons.append("region_out_of_range")
        except ValueError:
            reasons.append("malformed_region_id")
        if reasons:
            record = relation.to_dict()
            record["reasons"] = reasons
            dangling.append(record)
    return dangling


def align_relations(relations: list[Relation],
                    assertion_ids: set[str],
                    region_count: int,
                    strict: bool = False
                    ) -> tuple[list[Relation], list[dict]]:
    """Split relations into usable entries and dangling-entry records.

    ``strict=True`` raises :class:`PipelineError` on the first dangling entry;
    this is used for injected per-round dangling faults where the sample must
    fail. Static validation uses ``strict=False`` and records every entry.
    """
    dangling = validate_relations(relations, assertion_ids, region_count)
    if strict and dangling:
        entry = dangling[0]
        raise PipelineError(
            E_RELATION_DANGLING, STAGE_ALIGN,
            f"dangling relation {entry['relation_id']}: "
            f"{','.join(entry['reasons'])}")
    dangling_ids = {entry["relation_id"] for entry in dangling}
    usable = [relation for relation in relations
              if relation.relation_id not in dangling_ids]
    return usable, dangling


def assertion_region_map(relations: list[Relation]) -> dict[str, str]:
    """Primary region reference per assertion, based on associated relations."""
    mapping: dict[str, str] = {}
    for relation in relations:
        if relation.kind == RELATION_ASSOCIATED \
                and relation.assertion_id not in mapping:
            mapping[relation.assertion_id] = relation.region_id
    return mapping
