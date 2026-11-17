"""Independent fault-injection configuration and per-round injectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import FusionConfig
from .errors import (E_ASSERTION_MISSING, E_IMAGE_TRUNCATED,
                     E_NON_FINITE_FEATURE, E_REGION_OUT_OF_RANGE,
                     E_RELATION_DANGLING, PipelineError, STAGE_ALIGN,
                     STAGE_FUSION, STAGE_VISUAL)

FAULT_IMAGE_TRUNCATED = "image_truncated"
FAULT_ASSERTION_MISSING = "assertion_missing_field"
FAULT_REGION_OUT_OF_RANGE = "region_out_of_range"
FAULT_NON_FINITE = "non_finite_feature"
FAULT_RELATION_DANGLING = "relation_dangling"
KNOWN_FAULTS = (FAULT_IMAGE_TRUNCATED, FAULT_ASSERTION_MISSING,
                FAULT_REGION_OUT_OF_RANGE, FAULT_NON_FINITE,
                FAULT_RELATION_DANGLING)

#: Fault -> (business code, stage) routing used by the failure list.
FAULT_ROUTING = {
    FAULT_IMAGE_TRUNCATED: (E_IMAGE_TRUNCATED, STAGE_VISUAL),
    FAULT_ASSERTION_MISSING: (E_ASSERTION_MISSING, STAGE_ALIGN),
    FAULT_REGION_OUT_OF_RANGE: (E_REGION_OUT_OF_RANGE, STAGE_ALIGN),
    FAULT_NON_FINITE: (E_NON_FINITE_FEATURE, STAGE_FUSION),
    FAULT_RELATION_DANGLING: (E_RELATION_DANGLING, STAGE_ALIGN),
}


@dataclass(frozen=True)
class Injection:
    injection_id: str
    fault: str
    sample: str
    round: int

    def to_dict(self) -> dict[str, Any]:
        return {"injection_id": self.injection_id, "fault": self.fault,
                "sample": self.sample, "round": self.round}


def load_injection_config(path: str | None) -> list[Injection]:
    """Load the standalone injection file; ``None`` means no injections."""
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or "injections" not in document:
        raise ValueError("injection config must be an object with 'injections'")
    injections: list[Injection] = []
    seen = set()
    for entry in document["injections"]:
        for field in ("injection_id", "fault", "sample", "round"):
            if field not in entry:
                raise ValueError(f"injection entry missing field: {field}")
        if entry["fault"] not in KNOWN_FAULTS:
            raise ValueError(f"unknown injected fault: {entry['fault']}")
        if not isinstance(entry["round"], int) or entry["round"] < 0:
            raise ValueError("injection round must be a non-negative int")
        if entry["injection_id"] in seen:
            raise ValueError(
                f"duplicate injection_id: {entry['injection_id']}")
        seen.add(entry["injection_id"])
        injections.append(Injection(
            injection_id=entry["injection_id"],
            fault=entry["fault"],
            sample=entry["sample"],
            round=entry["round"],
        ))
    return injections


def active_injections(injections: list[Injection], sample: str,
                      round_index: int) -> list[Injection]:
    """Stable, deterministic lookup of injections for one sample/round."""
    return sorted(
        (item for item in injections
         if item.sample == sample and item.round == round_index),
        key=lambda item: item.injection_id)


def apply_image_fault(png_bytes: bytes, injection: Injection) -> bytes:
    if injection.fault == FAULT_IMAGE_TRUNCATED:
        # Keep the PNG magic so the failure happens during verification/decode
        # rather than at header inspection.
        cut = max(8, len(png_bytes) // 2)
        return png_bytes[:cut]
    return png_bytes


def apply_assertion_fault(assertions: dict,
                          injection: Injection) -> dict:
    if injection.fault != FAULT_ASSERTION_MISSING:
        return assertions
    corrupted = json.loads(json.dumps(assertions))
    if corrupted["assertions"]:
        corrupted["assertions"][0].pop("attribute", None)
    return corrupted


def apply_relation_fault(relations: list, injection: Injection,
                         config: FusionConfig) -> list:
    """Return relations plus a synthetic dangling or out-of-range entry."""
    from .textrel import Relation

    if injection.fault == FAULT_RELATION_DANGLING:
        extra = Relation(
            relation_id=f"injected-{injection.injection_id}",
            kind="associated",
            assertion_id="assertion-does-not-exist",
            region_id="region-0",
            weight=1.0,
        )
    elif injection.fault == FAULT_REGION_OUT_OF_RANGE:
        first = relations[0] if relations else None
        extra = Relation(
            relation_id=f"injected-{injection.injection_id}",
            kind="associated",
            assertion_id=first.assertion_id if first else "assertion-x",
            region_id=f"region-{config.region_count + 3}",
            weight=1.0,
        )
    else:
        return relations
    return relations + [extra]


def apply_feature_fault(features, injection: Injection):
    if injection.fault == FAULT_NON_FINITE:
        corrupted = features.copy()
        corrupted[0, 0] = float("inf")
        return corrupted
    return features


def routing_for(fault: str) -> tuple[str, str]:
    return FAULT_ROUTING[fault]


def ensure_routed(fault: str, exc: PipelineError) -> PipelineError:
    """Tag an exception with the injection's canonical code/stage."""
    code, stage = FAULT_ROUTING[fault]
    return PipelineError(code, stage, str(exc))
