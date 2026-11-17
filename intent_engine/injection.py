"""Deterministic fault injection driven by a separate JSON file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import STAGE_ORDER
from .utils import read_json, stable_hash

FAULT_ALIASES = {
    "empty": "empty_sentence",
    "empty_text": "empty_sentence",
    "empty_sentence": "empty_sentence",
    "missing_chars": "missing_chars",
    "char_sequence_missing": "missing_chars",
    "remove_entity": "remove_entity",
    "entity_removal": "remove_entity",
    "rule_conflict": "rule_conflict_pair",
    "rule_conflict_pair": "rule_conflict_pair",
    "confidence_threshold": "confidence_threshold",
    "confidence_eq_threshold": "confidence_threshold",
    "nonfinite_weight": "nonfinite_weight",
    "nan_weight": "nonfinite_weight",
    "infinite_weight": "nonfinite_weight",
}

STAGE_ALIASES = {
    "load": "normalize",
    "ingest": "normalize",
    "normalize": "normalize",
    "normalization": "normalize",
    "score": "intent",
    "scoring": "intent",
    "intent": "intent",
    "intent_score": "intent",
    "slot": "slots",
    "slots": "slots",
    "extract": "slots",
    "seq": "sequence",
    "sequence": "sequence",
    "bieso": "sequence",
    "merge": "merge",
}


@dataclass(frozen=True)
class Injection:
    injection_id: str
    sample_id: str
    stage: str
    fault: str
    options: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "sample_id": self.sample_id,
            "stage": self.stage,
            "fault": self.fault,
            "options": self.options,
        }


@dataclass
class InjectionConfig:
    injections: tuple[Injection, ...] = ()
    path: str | None = None
    config_hash: str = stable_hash([])

    def for_sample_stage(self, sample_id: str, stage: str) -> tuple[Injection, ...]:
        canonical_stage = STAGE_ALIASES.get(stage, stage)
        return tuple(
            item
            for item in self.injections
            if item.sample_id == sample_id and item.stage == canonical_stage
        )

    def for_sample(self, sample_id: str) -> tuple[Injection, ...]:
        return tuple(item for item in self.injections if item.sample_id == sample_id)

    def ids_for_sample(self, sample_id: str) -> list[str]:
        return [item.injection_id for item in self.for_sample(sample_id)]

    def as_report(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.injections]


def _parse_injections(raw: Any) -> tuple[list[Injection], str | None]:
    if raw is None:
        return [], None
    path = None
    if isinstance(raw, dict) and "injections" in raw:
        path = raw.get("source_path")
        items = raw["injections"]
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = []
        for value in raw.values():
            if isinstance(value, list):
                items.extend(value)
            elif isinstance(value, dict):
                items.append(value)
    else:
        raise ValueError("injection config must be a list or an object containing 'injections'")
    if not isinstance(items, list):
        raise ValueError("injections must be a list")
    result: list[Injection] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("each injection must be an object")
        injection_id = item.get("id", item.get("injection_id", item.get("item_id")))
        if not isinstance(injection_id, str) or not injection_id.strip():
            raise ValueError(f"injection at index {index} needs a non-empty id")
        injection_id = injection_id.strip()
        if injection_id in seen:
            raise ValueError(f"duplicate injection id: {injection_id}")
        seen.add(injection_id)
        sample_id = item.get("sample_id", item.get("sample", item.get("id_sample")))
        if not isinstance(sample_id, str) or not sample_id:
            raise ValueError(f"injection {injection_id} needs sample_id")
        stage_raw = str(item.get("stage", "normalize"))
        stage = STAGE_ALIASES.get(stage_raw, stage_raw)
        if stage not in STAGE_ORDER:
            raise ValueError(f"injection {injection_id} has unsupported stage: {stage_raw}")
        fault_raw = str(item.get("fault", item.get("type", item.get("kind", ""))))
        fault = FAULT_ALIASES.get(fault_raw)
        if fault is None:
            raise ValueError(f"injection {injection_id} has unsupported fault: {fault_raw!r}")
        options = {
            key: value
            for key, value in item.items()
            if key not in {"id", "injection_id", "item_id", "sample_id", "sample", "stage", "fault", "type", "kind"}
        }
        result.append(
            Injection(
                injection_id=injection_id,
                sample_id=sample_id,
                stage=stage,
                fault=fault,
                options=options,
            )
        )
    result.sort(key=lambda item: (item.sample_id, STAGE_ORDER[item.stage], item.injection_id))
    if path is not None:
        raise ValueError("source_path is an internal field and may not be supplied")
    return result, path


def load_injections(path: str | Path | None) -> InjectionConfig:
    if path is None:
        return InjectionConfig()
    raw = read_json(path)
    injections, _ = _parse_injections(raw)
    return InjectionConfig(
        injections=tuple(injections),
        path=str(path),
        config_hash=stable_hash(raw),
    )


def parse_injections(raw: Any) -> InjectionConfig:
    injections, _ = _parse_injections(raw)
    return InjectionConfig(injections=tuple(injections), config_hash=stable_hash(raw))


def all_injection_ids(injections: Iterable[Injection]) -> list[str]:
    return sorted({item.injection_id for item in injections})

