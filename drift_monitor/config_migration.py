"""Bidirectional migration between the v1, v2 and v3 configuration dialects.

Upgrades from v1/v2 fill in the fields that the older dialect cannot express
(test method chain, quantile caliber, minimum sample count).  Every fill is
recorded in ``provenance`` together with its source.  Downgrades to v1 follow
an explicit ``drop`` or ``freeze`` policy and always emit a list describing
exactly which capability was discarded.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from typing import Any, Dict, List, Tuple

from .config_model import (
    CATEGORICAL,
    DEFAULT_METHOD,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_QUANTILES,
    MEAN_SHIFT,
    NUMERIC,
    PROPORTION_DIST,
    QUANTILE_DIST,
    FeatureSpec,
    MonitorConfig,
    specs_equal,
)
from .errors import DriftError, ErrorCode

DROP_POLICY = "drop"
FREEZE_POLICY = "freeze"


def _clone(config: MonitorConfig, source_format: str) -> MonitorConfig:
    return MonitorConfig(
        config_id=config.config_id,
        source_format=source_format,
        features={name: replace(spec) for name, spec in config.features.items()},
        provenance=copy.deepcopy(config.provenance),
    )


def _record(
    config: MonitorConfig,
    feature: str,
    field: str,
    source: str,
    value: Any,
) -> None:
    config.provenance.append(
        {
            "feature": feature,
            "field": field,
            "action": "default_completed",
            "source": source,
            "value": value,
        }
    )


def _default_methods(spec: FeatureSpec) -> List[str]:
    if spec.column_type == NUMERIC:
        return [MEAN_SHIFT]
    return [PROPORTION_DIST]


def upgrade_to_v2(config: MonitorConfig) -> MonitorConfig:
    target = _clone(config, "v2")
    for name in target.feature_names():
        spec = target.features[name]
        if not spec.methods:
            spec.methods = _default_methods(spec)
            _record(target, name, "methods", "builtin:v2_default_methods", list(spec.methods))
        if spec.column_type == NUMERIC and not spec.quantiles:
            spec.quantiles = list(DEFAULT_QUANTILES)
            _record(target, name, "quantiles", "builtin:v2_default_quantiles", list(spec.quantiles))
        if spec.min_samples <= 0:
            spec.min_samples = DEFAULT_MIN_SAMPLES
            _record(target, name, "min_samples", "builtin:v2_default_min_samples", spec.min_samples)
    target.validate()
    return target


def upgrade_to_v3(config: MonitorConfig) -> MonitorConfig:
    # v3 is a strict superset of v2, so normalize through the v2 completion
    # rules first to guarantee a single source of defaulting behavior.
    normalized = upgrade_to_v2(config) if config.source_format == "v1" else config
    target = _clone(normalized, "v3")
    for name in target.feature_names():
        spec = target.features[name]
        if not spec.methods:
            spec.methods = _default_methods(spec)
            _record(target, name, "methods", "builtin:v3_default_methods", list(spec.methods))
        if spec.column_type == NUMERIC and not spec.quantiles:
            spec.quantiles = list(DEFAULT_QUANTILES)
            _record(target, name, "quantiles", "builtin:v3_default_quantiles", list(spec.quantiles))
        if spec.min_samples <= 0:
            spec.min_samples = DEFAULT_MIN_SAMPLES
            _record(target, name, "min_samples", "builtin:v3_default_min_samples", spec.min_samples)
    target.validate()
    return target


def downgrade_to_v2(config: MonitorConfig) -> MonitorConfig:
    target = _clone(config, "v2")
    target.validate()
    return target


def downgrade_to_v1(
    config: MonitorConfig,
    policy: str = FREEZE_POLICY,
) -> Tuple[MonitorConfig, List[Dict[str, Any]]]:
    """Return a v1-capable config and the list of discarded capabilities."""

    if policy not in (DROP_POLICY, FREEZE_POLICY):
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            f"downgrade policy must be 'drop' or 'freeze', got {policy!r}",
            {"field": "downgrade_policy"},
        )

    target = _clone(config, "v1")
    discarded: List[Dict[str, Any]] = []
    for name in target.feature_names():
        spec = target.features[name]
        if spec.column_type == NUMERIC:
            # v1 can only express the plain mean-shift check.
            extra = [m for m in spec.methods if m != MEAN_SHIFT]
            if extra:
                discarded.append(
                    {
                        "feature": name,
                        "field": "methods",
                        "dropped": list(extra),
                        "policy": policy,
                    }
                )
            if policy == FREEZE_POLICY:
                spec.methods = [MEAN_SHIFT]
            else:  # drop: remove the un-expressible methods entirely
                spec.methods = [m for m in spec.methods if m == MEAN_SHIFT] or [MEAN_SHIFT]
        if spec.quantiles:
            discarded.append(
                {
                    "feature": name,
                    "field": "quantiles",
                    "dropped": list(spec.quantiles),
                    "policy": policy,
                }
            )
            spec.quantiles = []
        if spec.min_samples != DEFAULT_MIN_SAMPLES:
            discarded.append(
                {
                    "feature": name,
                    "field": "min_samples",
                    "dropped": spec.min_samples,
                    "policy": policy,
                }
            )
            spec.min_samples = DEFAULT_MIN_SAMPLES
    target.validate()
    target.provenance.append(
        {
            "feature": "*",
            "field": "dialect",
            "action": "downgrade_to_v1",
            "policy": policy,
            "discarded_count": len(discarded),
        }
    )
    return target, discarded


def serialize_v1(config: MonitorConfig) -> str:
    lines = ["# DRIFT_MONITOR_CONFIG v1", "# name|type|threshold|window|enabled"]
    for name in config.feature_names():
        spec = config.features[name]
        lines.append(
            "|".join(
                [
                    spec.name,
                    spec.column_type,
                    _format_float(spec.threshold),
                    str(int(spec.window)),
                    "true" if spec.enabled else "false",
                ]
            )
        )
    return "\n".join(lines) + "\n"


def serialize_v2(config: MonitorConfig) -> str:
    lines = [
        "# DRIFT_MONITOR_CONFIG v2",
        "# key=value pipe separated rows; methods/quantiles are comma lists",
    ]
    for name in config.feature_names():
        spec = config.features[name]
        pairs = [
            f"name={spec.name}",
            f"type={spec.column_type}",
            f"threshold={_format_float(spec.threshold)}",
            f"window={int(spec.window)}",
            f"enabled={'true' if spec.enabled else 'false'}",
            f"methods={','.join(spec.methods)}",
        ]
        if spec.column_type == NUMERIC:
            pairs.append(f"quantiles={','.join(_format_float(q) for q in spec.quantiles)}")
        pairs.append(f"min_samples={int(spec.min_samples)}")
        lines.append("|".join(pairs))
    return "\n".join(lines) + "\n"


def serialize_v3(config: MonitorConfig) -> str:
    document = {
        "format_version": "v3",
        "config_id": config.config_id,
        "features": [
            {
                "name": spec.name,
                "type": spec.column_type,
                "threshold": spec.threshold,
                "window": spec.window,
                "methods": list(spec.methods),
                "quantiles": list(spec.quantiles),
                "min_samples": spec.min_samples,
                "enabled": spec.enabled,
            }
            for spec in (config.features[n] for n in config.feature_names())
        ],
    }
    return json.dumps(document, sort_keys=True, indent=2) + "\n"


def assert_roundtrip(original: MonitorConfig, recovered: MonitorConfig) -> None:
    """Raise E_ROUNDTRIP_MISMATCH on the first feature that differs."""

    original_names = set(original.features)
    recovered_names = set(recovered.features)
    if original_names != recovered_names:
        first = sorted(original_names.symmetric_difference(recovered_names))[0]
        raise DriftError(
            ErrorCode.ROUNDTRIP_MISMATCH,
            "feature set changed during round trip",
            {"feature": first},
        )
    for name in original.feature_names():
        before = original.features[name]
        after = recovered.features[name]
        if not specs_equal(before, after):
            mismatched = _first_mismatch(before, after)
            raise DriftError(
                ErrorCode.ROUNDTRIP_MISMATCH,
                f"feature {name!r} differs after round trip",
                {"feature": name, "field": mismatched},
            )


def _first_mismatch(before: FeatureSpec, after: FeatureSpec) -> str:
    for field_name in (
        "column_type",
        "threshold",
        "window",
        "methods",
        "quantiles",
        "min_samples",
        "enabled",
    ):
        if getattr(before, field_name) != getattr(after, field_name):
            return field_name
    return "unknown"


def _format_float(value: float) -> str:
    text = repr(float(value))
    return text
