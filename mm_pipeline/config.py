"""Collaborative reasoning configuration, evidence relations and hashing."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

RELATION_ASSOCIATE = "associate"
RELATION_CONFLICT = "conflict"
RELATION_SUPPLEMENT = "supplement"
RELATION_TYPES = (RELATION_ASSOCIATE, RELATION_CONFLICT, RELATION_SUPPLEMENT)


class ConfigError(ValueError):
    """Raised when configuration or relation table content is invalid."""


def canonical_hash(obj) -> str:
    """Stable SHA-256 over canonical JSON (independent of dict ordering)."""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class PipelineConfig:
    feature_dim: int
    quantization_levels: int
    text_evidence_weight: float
    fusion_temperature: float
    confidence_threshold: float
    max_evidence_rounds: int
    concurrency: int
    grid_rows: int = 4
    grid_cols: int = 4
    conflict_penalty: float = 0.25

    raw: dict = field(default_factory=dict)
    config_hash: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config JSON invalid: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a JSON object")

        required = [
            "feature_dim", "quantization_levels", "text_evidence_weight",
            "fusion_temperature", "confidence_threshold",
            "max_evidence_rounds", "concurrency",
        ]
        missing = [key for key in required if key not in raw]
        if missing:
            raise ConfigError(f"missing config keys: {missing}")

        cfg = cls(
            feature_dim=int(raw["feature_dim"]),
            quantization_levels=int(raw["quantization_levels"]),
            text_evidence_weight=float(raw["text_evidence_weight"]),
            fusion_temperature=float(raw["fusion_temperature"]),
            confidence_threshold=float(raw["confidence_threshold"]),
            max_evidence_rounds=int(raw["max_evidence_rounds"]),
            concurrency=int(raw["concurrency"]),
            grid_rows=int(raw.get("grid_rows", 4)),
            grid_cols=int(raw.get("grid_cols", 4)),
            conflict_penalty=float(raw.get("conflict_penalty", 0.25)),
            raw=raw,
        )
        cfg._validate()
        cfg.config_hash = canonical_hash(raw)
        return cfg

    def _validate(self) -> None:
        if self.feature_dim < 4:
            raise ConfigError("feature_dim must be >= 4")
        if self.quantization_levels < 2:
            raise ConfigError("quantization_levels must be >= 2")
        if not 0.0 <= self.text_evidence_weight <= 1.0:
            raise ConfigError("text_evidence_weight must be within [0, 1]")
        if self.fusion_temperature <= 0:
            raise ConfigError("fusion_temperature must be > 0")
        if not 0.0 < self.confidence_threshold < 1.0:
            raise ConfigError("confidence_threshold must be within (0, 1)")
        if self.max_evidence_rounds < 1:
            raise ConfigError("max_evidence_rounds must be >= 1")
        if self.concurrency < 1:
            raise ConfigError("concurrency must be >= 1")
        if self.grid_rows < 1 or self.grid_cols < 1:
            raise ConfigError("grid dimensions must be positive")
        if not 0.0 <= self.conflict_penalty <= 1.0:
            raise ConfigError("conflict_penalty must be within [0, 1]")


def load_relations(path: str | Path) -> list[dict]:
    """Load and structurally validate the evidence relation table."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"relation file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"relation JSON invalid: {exc}") from exc
    if not isinstance(data, list):
        raise ConfigError("relation table root must be a JSON array")

    relations: list[dict] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigError(f"relation #{index} must be an object")
        missing = [key for key in ("relation_id", "type", "assertion_id",
                                   "region_id") if key not in item]
        if missing:
            raise ConfigError(f"relation #{index} missing keys {missing}")
        rel = {
            "relation_id": str(item["relation_id"]),
            "type": str(item["type"]),
            "assertion_id": str(item["assertion_id"]),
            "region_id": str(item["region_id"]),
            "weight": float(item.get("weight", 1.0)),
        }
        if rel["relation_id"] in seen_ids:
            raise ConfigError(f"duplicate relation_id {rel['relation_id']}")
        seen_ids.add(rel["relation_id"])
        if rel["type"] not in RELATION_TYPES:
            raise ConfigError(
                f"relation {rel['relation_id']} has invalid type {rel['type']}")
        relations.append(rel)
    return relations
