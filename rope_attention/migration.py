"""Explicit, auditable promotions and demotions between config versions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import parse_config
from .errors import E_SEMANTIC_DRIFT, E_VERSION_UNSUPPORTED, RopeError
from .models import (
    CONFIG_VERSION_V1,
    CONFIG_VERSION_V2,
    CONFIG_VERSION_V3,
    FrequencyAdjustment,
    ParsedConfig,
    SUPPORTED_CONFIG_VERSIONS,
)
from .util import canonical_json_bytes, format_float


@dataclass(frozen=True)
class MigrationResult:
    source_version: str
    target_version: str
    ok: bool
    business_code: str
    semantic_drift: bool
    target_text: str | None
    target_config: ParsedConfig | None
    chain: tuple[str, ...]
    fill_details: tuple[dict[str, Any], ...]
    discard_details: tuple[dict[str, Any], ...]
    unrecoverable_fields: tuple[str, ...]
    field_provenance: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_code": self.business_code,
            "chain": list(self.chain),
            "discard_details": list(self.discard_details),
            "field_provenance": dict(sorted(self.field_provenance.items())),
            "fill_details": list(self.fill_details),
            "ok": self.ok,
            "semantic_drift": self.semantic_drift,
            "source_version": self.source_version,
            "target_config": (
                self.target_config.to_dict() if self.target_config is not None else None
            ),
            "target_checksum": (
                self.target_config.content_checksum()
                if self.target_config is not None
                else None
            ),
            "target_text": self.target_text,
            "target_version": self.target_version,
            "unrecoverable_fields": list(self.unrecoverable_fields),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())


def _v1_text(
    *,
    base_theta: float,
    pos_scale: float,
    block_size: int,
) -> str:
    return (
        "v1\n"
        f"{format_float(base_theta)}|{format_float(pos_scale)}|{block_size}\n"
    )


def _v2_text(
    *,
    base_theta: float,
    pos_scale: float,
    block_size: int,
    variant: str,
) -> str:
    return (
        "v2\n"
        f"base_theta={format_float(base_theta)}"
        f"|pos_scale={format_float(pos_scale)}"
        f"|block_size={block_size}"
        f"|variant={variant}\n"
    )


def _v3_text(
    *,
    base_theta: float,
    pos_scale: float,
    block_size: int,
    variant: str,
    frequency_adjustment: FrequencyAdjustment,
    per_head_scale: list[float],
) -> str:
    document = {
        "base_theta": base_theta,
        "block_size": block_size,
        "frequency_adjustment": frequency_adjustment.to_dict(),
        "per_head_scale": per_head_scale,
        "pos_scale": pos_scale,
        "variant": variant,
    }
    return "v3\n" + json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"


def _new_state(config: ParsedConfig) -> dict[str, Any]:
    return {
        "base_theta": config.base_theta,
        "block_size": config.block_size,
        "frequency_adjustment": config.effective_frequency_adjustment,
        "per_head_scale": list(config.effective_per_head_scale),
        "pos_scale": config.pos_scale,
        "variant": config.effective_variant,
    }


def _append_discard(
    details: list[dict[str, Any]],
    unrecoverable: set[str],
    *,
    field: str,
    original: Any,
    replacement: Any,
    rule: str,
    recoverable: bool,
    step: str,
) -> None:
    details.append(
        {
            "field": field,
            "original": original,
            "recoverable": recoverable,
            "replacement": replacement,
            "rule": rule,
            "step": step,
        }
    )
    if not recoverable:
        unrecoverable.add(field)


def _target_for_version(version: str, state: dict[str, Any]) -> str:
    if version == CONFIG_VERSION_V1:
        return _v1_text(
            base_theta=float(state["base_theta"]),
            pos_scale=float(state["pos_scale"]),
            block_size=int(state["block_size"]),
        )
    if version == CONFIG_VERSION_V2:
        return _v2_text(
            base_theta=float(state["base_theta"]),
            pos_scale=float(state["pos_scale"]),
            block_size=int(state["block_size"]),
            variant=str(state["variant"]),
        )
    if version == CONFIG_VERSION_V3:
        return _v3_text(
            base_theta=float(state["base_theta"]),
            pos_scale=float(state["pos_scale"]),
            block_size=int(state["block_size"]),
            variant=str(state["variant"]),
            frequency_adjustment=state["frequency_adjustment"],
            per_head_scale=[float(value) for value in state["per_head_scale"]],
        )
    raise ValueError(version)


def _step_v1_to_v2(
    state: dict[str, Any],
    *,
    fills: list[dict[str, Any]],
    provenance: dict[str, str],
) -> None:
    fills.append(
        {
            "field": "variant",
            "source": "v1_implicit_rope_default",
            "step": "v1->v2",
            "value": "rope",
        }
    )
    provenance["variant"] = "filled:v1_implicit_rope_default"
    state["variant"] = "rope"
    provenance.setdefault("block_size", "retained:source")


def _step_v2_to_v3(
    state: dict[str, Any],
    *,
    fills: list[dict[str, Any]],
    provenance: dict[str, str],
    source_variant_was_explicit: bool,
) -> None:
    variant_provenance = provenance.get("variant", "absent")
    if not source_variant_was_explicit and not variant_provenance.startswith("filled:"):
        fills.append(
            {
                "field": "variant",
                "source": "v2_schema_default_when_absent",
                "step": "v2->v3",
                "value": "rope",
            }
        )
        provenance["variant"] = "filled:v2_schema_default_when_absent"
        state["variant"] = "rope"
    else:
        provenance["variant"] = variant_provenance

    fills.append(
        {
            "field": "frequency_adjustment",
            "source": "v3_schema_identity_default",
            "step": "v2->v3",
            "value": FrequencyAdjustment().to_dict(),
        }
    )
    provenance["frequency_adjustment"] = "filled:v3_schema_identity_default"
    state["frequency_adjustment"] = FrequencyAdjustment()

    fills.append(
        {
            "field": "per_head_scale",
            "source": "v3_schema_unity_default",
            "step": "v2->v3",
            "value": [1.0],
        }
    )
    provenance["per_head_scale"] = "filled:v3_schema_unity_default"
    state["per_head_scale"] = [1.0]
    provenance.setdefault("block_size", "retained:source")


def _step_v3_to_v2(
    state: dict[str, Any],
    *,
    discards: list[dict[str, Any]],
    unrecoverable: set[str],
    provenance: dict[str, str],
) -> None:
    source_variant = str(state["variant"])
    if source_variant == "block_freq":
        _append_discard(
            discards,
            unrecoverable,
            field="variant",
            original=source_variant,
            replacement="rope",
            rule="v2_cannot_express_block_freq_fallback_to_rope",
            recoverable=False,
            step="v3->v2",
        )
        state["variant"] = "rope"
        provenance["variant"] = "degraded:block_freq_to_rope"
    else:
        provenance["variant"] = "retained:source"

    frequency = state["frequency_adjustment"]
    if frequency.method == "identity":
        _append_discard(
            discards,
            unrecoverable,
            field="frequency_adjustment",
            original=frequency.to_dict(),
            replacement="omitted_identity",
            rule="identity_adjustment_has_no_effect",
            recoverable=True,
            step="v3->v2",
        )
    elif frequency.method == "global_scale":
        state["pos_scale"] = float(state["pos_scale"]) * float(frequency.factor)
        _append_discard(
            discards,
            unrecoverable,
            field="frequency_adjustment",
            original=frequency.to_dict(),
            replacement={
                "pos_scale": state["pos_scale"],
                "rule": "factor_is_folded_into_pos_scale",
            },
            rule="global_frequency_factor_folded_into_pos_scale",
            recoverable=True,
            step="v3->v2",
        )
    else:
        _append_discard(
            discards,
            unrecoverable,
            field="frequency_adjustment",
            original=frequency.to_dict(),
            replacement="dropped",
            rule="v2_cannot_express_position_blockwise_frequency_adjustment",
            recoverable=False,
            step="v3->v2",
        )

    scales = [float(value) for value in state["per_head_scale"]]
    if len(set(scales)) == 1:
        folded = scales[0]
        state["pos_scale"] = float(state["pos_scale"]) * folded
        _append_discard(
            discards,
            unrecoverable,
            field="per_head_scale",
            original=scales,
            replacement={
                "pos_scale": state["pos_scale"],
                "rule": "uniform_head_scale_is_folded_into_pos_scale",
            },
            rule="uniform_head_scale_folded_into_pos_scale",
            recoverable=True,
            step="v3->v2",
        )
    else:
        mean_scale = sum(scales) / len(scales)
        state["pos_scale"] = float(state["pos_scale"]) * mean_scale
        _append_discard(
            discards,
            unrecoverable,
            field="per_head_scale",
            original=scales,
            replacement={
                "pos_scale": state["pos_scale"],
                "rule": "mean_head_scale_is_folded_into_pos_scale",
            },
            rule="nonuniform_head_scale_flattened_to_mean_and_residuals_dropped",
            recoverable=False,
            step="v3->v2",
        )
    provenance["frequency_adjustment"] = "dropped:v2_cannot_express"
    provenance["per_head_scale"] = "dropped:v2_cannot_express"


def _step_v2_to_v1(
    state: dict[str, Any],
    *,
    discards: list[dict[str, Any]],
    unrecoverable: set[str],
    provenance: dict[str, str],
) -> None:
    variant = str(state["variant"])
    if variant == "rope":
        _append_discard(
            discards,
            unrecoverable,
            field="variant",
            original=variant,
            replacement="implicit_rope",
            rule="v1_implicitly_means_rope",
            recoverable=True,
            step="v2->v1",
        )
        provenance["variant"] = "dropped:v1_implicit_rope"
    else:
        _append_discard(
            discards,
            unrecoverable,
            field="variant",
            original=variant,
            replacement="implicit_rope",
            rule="v1_cannot_express_non_rope_variant",
            recoverable=False,
            step="v2->v1",
        )
        provenance["variant"] = "degraded:non_rope_to_implicit_rope"
        state["variant"] = "rope"


def migrate(
    source: ParsedConfig | str | bytes,
    target_version: str,
    *,
    allow_semantic_drift: bool = True,
) -> MigrationResult:
    """Migrate with explicit promotion fills and demotion loss records."""

    if target_version not in SUPPORTED_CONFIG_VERSIONS:
        raise RopeError(
            E_VERSION_UNSUPPORTED,
            "target config version is not supported",
            details={
                "target_version": target_version,
                "supported_versions": list(SUPPORTED_CONFIG_VERSIONS),
            },
        )
    parsed = parse_config(source) if isinstance(source, (str, bytes)) else source
    if parsed.source_version == target_version:
        return MigrationResult(
            source_version=parsed.source_version,
            target_version=target_version,
            ok=True,
            business_code="OK",
            semantic_drift=False,
            target_text=None,
            target_config=parsed,
            chain=(),
            fill_details=(),
            discard_details=(),
            unrecoverable_fields=(),
            field_provenance={
                name: "retained:source" for name in parsed.explicit_fields
            },
        )

    state = _new_state(parsed)
    fills: list[dict[str, Any]] = []
    discards: list[dict[str, Any]] = []
    unrecoverable: set[str] = set()
    chain: list[str] = []
    provenance: dict[str, str] = {
        "base_theta": "retained:source",
        "block_size": "retained:source",
        "frequency_adjustment": "retained:source"
        if parsed.frequency_adjustment is not None
        else "absent",
        "per_head_scale": "retained:source"
        if parsed.per_head_scale is not None
        else "absent",
        "pos_scale": "retained:source",
        "variant": "source:explicit" if parsed.variant is not None else "absent",
    }

    ascending = {CONFIG_VERSION_V1: 0, CONFIG_VERSION_V2: 1, CONFIG_VERSION_V3: 2}
    current = parsed.source_version
    while ascending[current] < ascending[target_version]:
        if current == CONFIG_VERSION_V1:
            _step_v1_to_v2(state, fills=fills, provenance=provenance)
            chain.append("v1->v2")
            current = CONFIG_VERSION_V2
        else:
            _step_v2_to_v3(
                state,
                fills=fills,
                provenance=provenance,
                source_variant_was_explicit=parsed.variant is not None,
            )
            chain.append("v2->v3")
            current = CONFIG_VERSION_V3

    while ascending[current] > ascending[target_version]:
        if current == CONFIG_VERSION_V3:
            _step_v3_to_v2(
                state,
                discards=discards,
                unrecoverable=unrecoverable,
                provenance=provenance,
            )
            chain.append("v3->v2")
            current = CONFIG_VERSION_V2
        else:
            _step_v2_to_v1(
                state,
                discards=discards,
                unrecoverable=unrecoverable,
                provenance=provenance,
            )
            chain.append("v2->v1")
            current = CONFIG_VERSION_V1

    target_text = _target_for_version(target_version, state)
    target_parsed = parse_config(target_text)
    drift = bool(unrecoverable)
    if drift and not allow_semantic_drift:
        return MigrationResult(
            source_version=parsed.source_version,
            target_version=target_version,
            ok=False,
            business_code=E_SEMANTIC_DRIFT,
            semantic_drift=True,
            target_text=None,
            target_config=None,
            chain=tuple(chain),
            fill_details=tuple(fills),
            discard_details=tuple(discards),
            unrecoverable_fields=tuple(sorted(unrecoverable)),
            field_provenance=provenance,
        )
    return MigrationResult(
        source_version=parsed.source_version,
        target_version=target_version,
        ok=True,
        business_code=E_SEMANTIC_DRIFT if drift else "OK",
        semantic_drift=drift,
        target_text=target_text,
        target_config=target_parsed,
        chain=tuple(chain),
        fill_details=tuple(fills),
        discard_details=tuple(discards),
        unrecoverable_fields=tuple(sorted(unrecoverable)),
        field_provenance=provenance,
    )

