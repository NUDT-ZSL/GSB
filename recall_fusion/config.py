"""Fusion/recall configuration with explicit defaults and env overrides.

A configuration may be supplied as a JSON file. Every knob also has an explicit
default in :data:`DEFAULT_CONFIG` and may be overridden through environment
variables (prefix ``RF_``) so that a bare ``python -m recall_fusion.cli demo``
is fully reproducible without any external setup.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .errors import BusinessError, E_CONFIG_INVALID

CHANNEL_INVERTED = "inverted"
CHANNEL_PREFIX = "prefix"
CHANNEL_CATEGORY = "category"
CHANNEL_VECTOR = "vector"
CHANNEL_ORDER = (CHANNEL_INVERTED, CHANNEL_PREFIX, CHANNEL_CATEGORY, CHANNEL_VECTOR)

NORM_MINMAX = "minmax"
NORM_RANK_INV = "rank_inverse"
VALID_NORMS = (NORM_MINMAX, NORM_RANK_INV)

DEDUP_HIGHEST_NORM = "highest_normalized"
DEDUP_FIRST_HIT = "first_hit"
VALID_DEDUPS = (DEDUP_HIGHEST_NORM, DEDUP_FIRST_HIT)

PRECISION_FP64 = "fp64"
PRECISION_FP32 = "fp32"
VALID_PRECISIONS = (PRECISION_FP64, PRECISION_FP32)


@dataclass(frozen=True)
class ChannelSpec:
    enabled: bool
    candidate_limit: int
    weight: float
    normalization: str = ""  # empty -> use global normalization


@dataclass(frozen=True)
class DataGenConfig:
    seed: int = 20261122
    num_docs: int = 400
    num_queries: int = 48
    num_terms: int = 60
    vector_dim: int = 32
    categories: tuple[str, ...] = ("alpha", "beta", "gamma", "delta")


@dataclass(frozen=True)
class Config:
    channels: Mapping[str, ChannelSpec]
    normalization: str
    dedup_policy: str
    precision: str
    workers: int
    batch_size: int
    total_candidate_budget: int
    fusion_compare_budget: int
    memory_limit_bytes: int
    degrade_priority: tuple[str, ...]
    policy_priority: tuple[str, ...]
    two_stage_enabled: bool
    coarse_k: int
    fine_k: int
    datagen: DataGenConfig

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # tuples -> lists for JSON friendliness
        data["degrade_priority"] = list(self.degrade_priority)
        data["policy_priority"] = list(self.policy_priority)
        data["datagen"]["categories"] = list(self.datagen.categories)
        return data


def _default_channels() -> dict[str, ChannelSpec]:
    return {
        CHANNEL_INVERTED: ChannelSpec(True, 40, 0.35),
        CHANNEL_PREFIX: ChannelSpec(True, 30, 0.20),
        CHANNEL_CATEGORY: ChannelSpec(True, 25, 0.15),
        CHANNEL_VECTOR: ChannelSpec(True, 35, 0.30),
    }


DEFAULT_CONFIG = Config(
    channels=_default_channels(),
    normalization=NORM_MINMAX,
    dedup_policy=DEDUP_HIGHEST_NORM,
    precision=PRECISION_FP64,
    workers=2,
    batch_size=8,
    total_candidate_budget=5600,
    fusion_compare_budget=4000,
    memory_limit_bytes=180 * 1024,
    degrade_priority=(CHANNEL_VECTOR, CHANNEL_PREFIX, CHANNEL_CATEGORY, CHANNEL_INVERTED),
    policy_priority=("memory", "budget", "quality", "latency"),
    two_stage_enabled=True,
    coarse_k=40,
    fine_k=12,
    datagen=DataGenConfig(),
)


_ENV_STR = {
    "RF_NORMALIZATION": ("normalization", str),
    "RF_DEDUP_POLICY": ("dedup_policy", str),
    "RF_PRECISION": ("precision", str),
}
_ENV_INT = (
    ("RF_WORKERS", "workers"),
    ("RF_BATCH_SIZE", "batch_size"),
    ("RF_TOTAL_CANDIDATE_BUDGET", "total_candidate_budget"),
    ("RF_FUSION_COMPARE_BUDGET", "fusion_compare_budget"),
    ("RF_MEMORY_LIMIT_BYTES", "memory_limit_bytes"),
    ("RF_SEED", ("datagen", "seed")),
    ("RF_NUM_DOCS", ("datagen", "num_docs")),
    ("RF_NUM_QUERIES", ("datagen", "num_queries")),
    ("RF_VECTOR_DIM", ("datagen", "vector_dim")),
    ("RF_COARSE_K", "coarse_k"),
    ("RF_FINE_K", "fine_k"),
)


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    for env, (key, caster) in _ENV_STR.items():
        if env in os.environ:
            raw[key] = caster(os.environ[env])
    for env, path in _ENV_INT:
        if env in os.environ:
            value = int(os.environ[env])
            if isinstance(path, tuple):
                raw.setdefault("datagen", {})[path[1]] = value
            else:
                raw[path] = value
    if "RF_DEGRADE_PRIORITY" in os.environ:
        raw["degrade_priority"] = os.environ["RF_DEGRADE_PRIORITY"].split(",")
    if "RF_POLICY_PRIORITY" in os.environ:
        raw["policy_priority"] = os.environ["RF_POLICY_PRIORITY"].split(",")
    if "RF_TWO_STAGE" in os.environ:
        raw["two_stage_enabled"] = os.environ["RF_TWO_STAGE"].lower() in ("1", "true", "yes")
    return raw


def load_config(path: str | None = None) -> Config:
    raw: dict[str, Any] = DEFAULT_CONFIG.to_dict()
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            user_raw = json.load(handle)
        raw.update(user_raw)
    raw = _apply_env_overrides(raw)
    return _build_config(raw)


def _build_config(raw: Mapping[str, Any]) -> Config:
    stage = "config"
    if raw["normalization"] not in VALID_NORMS:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"normalization must be one of {VALID_NORMS}, got {raw['normalization']!r}",
            stage,
            "normalization",
        )
    if raw["dedup_policy"] not in VALID_DEDUPS:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"dedup_policy must be one of {VALID_DEDUPS}, got {raw['dedup_policy']!r}",
            stage,
            "dedup_policy",
        )
    if raw["precision"] not in VALID_PRECISIONS:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"precision must be one of {VALID_PRECISIONS}, got {raw['precision']!r}",
            stage,
            "precision",
        )

    channels: dict[str, ChannelSpec] = {}
    for name in CHANNEL_ORDER:
        spec = raw["channels"].get(name)
        if spec is None:
            raise BusinessError(
                E_CONFIG_INVALID, "missing channel spec", stage, f"channels.{name}"
            )
        norm = spec.get("normalization", "")
        if norm and norm not in VALID_NORMS:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"channel normalization must be one of {VALID_NORMS} or empty, got {norm!r}",
                stage,
                f"channels.{name}.normalization",
            )
        weight = float(spec["weight"])
        if weight < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"fusion weight must be non-negative, got {weight}",
                stage,
                f"channels.{name}.weight",
            )
        limit = int(spec["candidate_limit"])
        if limit < 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"candidate_limit must be non-negative, got {limit}",
                stage,
                f"channels.{name}.candidate_limit",
            )
        channels[name] = ChannelSpec(
            enabled=bool(spec["enabled"]),
            candidate_limit=limit,
            weight=weight,
            normalization=norm,
        )

    for key in ("workers", "batch_size"):
        if int(raw[key]) < 1:
            raise BusinessError(
                E_CONFIG_INVALID, f"{key} must be >= 1", stage, key
            )
    for key in (
        "total_candidate_budget",
        "fusion_compare_budget",
        "memory_limit_bytes",
    ):
        if int(raw[key]) < 0:
            raise BusinessError(
                E_CONFIG_INVALID, f"{key} must be >= 0", stage, key
            )

    degrade = tuple(raw["degrade_priority"])
    unknown = [c for c in degrade if c not in CHANNEL_ORDER]
    if unknown or set(degrade) != set(CHANNEL_ORDER):
        raise BusinessError(
            E_CONFIG_INVALID,
            "degrade_priority must be a permutation of all four channels",
            stage,
            "degrade_priority",
        )
    policy = tuple(raw["policy_priority"])
    if set(policy) != {"memory", "budget", "quality", "latency"}:
        raise BusinessError(
            E_CONFIG_INVALID,
            "policy_priority must be a permutation of memory,budget,quality,latency",
            stage,
            "policy_priority",
        )

    dg_raw = raw["datagen"]
    datagen = DataGenConfig(
        seed=int(dg_raw["seed"]),
        num_docs=int(dg_raw["num_docs"]),
        num_queries=int(dg_raw["num_queries"]),
        num_terms=int(dg_raw["num_terms"]),
        vector_dim=int(dg_raw["vector_dim"]),
        categories=tuple(dg_raw["categories"]),
    )

    return Config(
        channels=channels,
        normalization=raw["normalization"],
        dedup_policy=raw["dedup_policy"],
        precision=raw["precision"],
        workers=int(raw["workers"]),
        batch_size=int(raw["batch_size"]),
        total_candidate_budget=int(raw["total_candidate_budget"]),
        fusion_compare_budget=int(raw["fusion_compare_budget"]),
        memory_limit_bytes=int(raw["memory_limit_bytes"]),
        degrade_priority=degrade,
        policy_priority=policy,
        two_stage_enabled=bool(raw["two_stage_enabled"]),
        coarse_k=int(raw["coarse_k"]),
        fine_k=int(raw["fine_k"]),
        datagen=datagen,
    )


def config_digest_payload(config: Config) -> dict[str, Any]:
    """Stable JSON serialization used both for snapshots and on-disk artifacts."""
    return config.to_dict()
