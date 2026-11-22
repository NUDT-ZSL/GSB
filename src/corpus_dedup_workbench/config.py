from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .errors import E_CONFIG_INVALID, WorkbenchError
from .util import content_hash, decimal_text

VALID_MODES = {"full", "digest_summary"}
VALID_THRESHOLD_POLICIES = {"strict", "tolerance_band"}
VALID_RESOURCE_POLICIES = {"error", "degrade"}


@dataclass(frozen=True)
class WorkbenchConfig:
    signature_length: int = 12
    similarity_threshold: str = "0.75"
    candidate_window: int = 4
    validation_mode: str = "full"
    seed: int = 20261123
    threshold_policy: str = "tolerance_band"
    threshold_tolerance: str = "0.000000001"
    summary_bands: int = 4
    values_per_band: int = 2
    digest_min_common_bands: int = 2
    normalization_version: str = "nfkc-casefold-token-v1"
    max_docs_per_batch: int = 256
    max_candidate_comparisons: int = 5000
    max_cache_entries: int = 128
    latency_budget_ms: float = 50.0
    resource_policy: str = "error"
    sample: dict[str, Any] = field(
        default_factory=lambda: {"shards": 4, "docs_per_shard": 18}
    )

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> WorkbenchConfig:
        known = {
            "signature_length",
            "similarity_threshold",
            "candidate_window",
            "validation_mode",
            "seed",
            "threshold_policy",
            "threshold_tolerance",
            "summary_bands",
            "values_per_band",
            "digest_min_common_bands",
            "normalization_version",
            "max_docs_per_batch",
            "max_candidate_comparisons",
            "max_cache_entries",
            "latency_budget_ms",
            "resource_policy",
            "sample",
        }
        values = {key: mapping[key] for key in known if key in mapping}
        config = cls(**values)
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> WorkbenchConfig:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                E_CONFIG_INVALID,
                f"无法读取配置: {exc}",
                stage="config_load",
            ) from exc
        if not isinstance(raw, dict):
            raise WorkbenchError(
                E_CONFIG_INVALID, "配置根节点必须是对象", stage="config_load"
            )
        return cls.from_mapping(raw)

    def validate(self) -> None:
        errors: list[str] = []
        if not isinstance(self.signature_length, int):
            errors.append("signature_length 必须为整数")
        if not isinstance(self.candidate_window, int) or self.candidate_window < 0:
            errors.append("candidate_window 必须为非负整数")
        if self.validation_mode not in VALID_MODES:
            errors.append(f"validation_mode 必须为 {sorted(VALID_MODES)}")
        if not isinstance(self.seed, int):
            errors.append("seed 必须为整数")
        if self.threshold_policy not in VALID_THRESHOLD_POLICIES:
            errors.append(
                f"threshold_policy 必须为 {sorted(VALID_THRESHOLD_POLICIES)}"
            )
        try:
            threshold = Decimal(str(self.similarity_threshold))
            tolerance = Decimal(str(self.threshold_tolerance))
            if not Decimal(0) <= threshold <= Decimal(1):
                errors.append("similarity_threshold 必须在 [0, 1]")
            if tolerance < 0:
                errors.append("threshold_tolerance 必须非负")
        except InvalidOperation:
            errors.append("阈值或容差不是合法十进制数")
        if not isinstance(self.summary_bands, int) or self.summary_bands <= 0:
            errors.append("summary_bands 必须为正整数")
        if not isinstance(self.values_per_band, int) or self.values_per_band <= 0:
            errors.append("values_per_band 必须为正整数")
        if (
            not isinstance(self.digest_min_common_bands, int)
            or self.digest_min_common_bands <= 0
            or self.digest_min_common_bands > self.summary_bands
        ):
            errors.append("digest_min_common_bands 必须在 [1, summary_bands]")
        for name, value in (
            ("max_docs_per_batch", self.max_docs_per_batch),
            ("max_candidate_comparisons", self.max_candidate_comparisons),
            ("max_cache_entries", self.max_cache_entries),
        ):
            if not isinstance(value, int) or value <= 0:
                errors.append(f"{name} 必须为正整数")
        if (
            not isinstance(self.latency_budget_ms, (int, float))
            or float(self.latency_budget_ms) <= 0
        ):
            errors.append("latency_budget_ms 必须为正数")
        if self.resource_policy not in VALID_RESOURCE_POLICIES:
            errors.append(f"resource_policy 必须为 {sorted(VALID_RESOURCE_POLICIES)}")
        if errors:
            raise WorkbenchError(
                E_CONFIG_INVALID,
                "配置校验失败",
                stage="config_validate",
                details={"errors": errors},
            )

    @property
    def threshold(self) -> Decimal:
        return Decimal(str(self.similarity_threshold))

    @property
    def tolerance(self) -> Decimal:
        return Decimal(str(self.threshold_tolerance))

    @property
    def summary_size(self) -> int:
        return self.summary_bands * self.values_per_band

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature_length": self.signature_length,
            "similarity_threshold": decimal_text(self.similarity_threshold),
            "candidate_window": self.candidate_window,
            "validation_mode": self.validation_mode,
            "seed": self.seed,
            "threshold_policy": self.threshold_policy,
            "threshold_tolerance": decimal_text(self.threshold_tolerance),
            "summary_bands": self.summary_bands,
            "values_per_band": self.values_per_band,
            "digest_min_common_bands": self.digest_min_common_bands,
            "normalization_version": self.normalization_version,
            "max_docs_per_batch": self.max_docs_per_batch,
            "max_candidate_comparisons": self.max_candidate_comparisons,
            "max_cache_entries": self.max_cache_entries,
            "latency_budget_ms": self.latency_budget_ms,
            "resource_policy": self.resource_policy,
            "sample": self.sample,
        }

    def replaced(self, updates: dict[str, Any]) -> WorkbenchConfig:
        merged = self.to_dict()
        merged.update(updates)
        return WorkbenchConfig.from_mapping(merged)

    def cache_descriptor(self) -> dict[str, Any]:
        return {
            "signature_length": self.signature_length,
            "similarity_threshold": decimal_text(self.similarity_threshold),
            "candidate_window": self.candidate_window,
            "validation_mode": self.validation_mode,
            "threshold_policy": self.threshold_policy,
            "threshold_tolerance": decimal_text(self.threshold_tolerance),
            "summary_bands": self.summary_bands,
            "values_per_band": self.values_per_band,
            "digest_min_common_bands": self.digest_min_common_bands,
            "normalization_version": self.normalization_version,
            "max_candidate_comparisons": self.max_candidate_comparisons,
            "resource_policy": self.resource_policy,
        }

    @property
    def config_hash(self) -> str:
        return content_hash(self.cache_descriptor())

