"""Annotation pipeline configuration with strict validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import E_CONFIG_INVALID, fail
from .models import canonical_json, is_valid_name, sha256_value


@dataclass
class GovernanceConfig:
    merge_strategy: str
    annotator_priority: list[str]
    alias_mapping: dict[str, str]
    max_tokens_per_doc: int
    max_types: int
    max_resident_votes: int
    retirement_policy: str
    retirement_substitutes: dict[str, str]
    resource_policy: str = "fail"
    label_set_version: str | None = None
    downgrade_policy: dict[str, str] = field(
        default_factory=lambda: {
            "nested_aliases": "flatten",
            "disabled_aliases": "discard",
            "bio_prefix_required": "flatten",
        }
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "merge_strategy": self.merge_strategy,
            "annotator_priority": list(self.annotator_priority),
            "alias_mapping": dict(sorted(self.alias_mapping.items())),
            "max_tokens_per_doc": self.max_tokens_per_doc,
            "max_types": self.max_types,
            "max_resident_votes": self.max_resident_votes,
            "retirement_policy": self.retirement_policy,
            "retirement_substitutes": dict(
                sorted(self.retirement_substitutes.items())
            ),
            "resource_policy": self.resource_policy,
            "label_set_version": self.label_set_version,
            "downgrade_policy": dict(sorted(self.downgrade_policy.items())),
        }

    def digest(self) -> str:
        return sha256_value(self.to_payload())


def _require_int(data: dict[str, Any], key: str, *, minimum: int = 1) -> int:
    if key not in data:
        fail(
            E_CONFIG_INVALID,
            f"missing required config field {key}",
            field=key,
            reason="required_field_missing",
        )
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail(
            E_CONFIG_INVALID,
            f"{key} must be an integer >= {minimum}",
            field=key,
            reason="invalid_field",
            value=value,
        )
    return value


def _string_map(
    data: dict[str, Any],
    key: str,
    *,
    required: bool,
) -> dict[str, str]:
    if key not in data:
        if required:
            fail(
                E_CONFIG_INVALID,
                f"missing required config field {key}",
                field=key,
                reason="required_field_missing",
            )
        return {}
    raw = data[key]
    if not isinstance(raw, dict):
        fail(
            E_CONFIG_INVALID,
            f"{key} must be an object",
            field=key,
            reason="invalid_field",
        )
    result: dict[str, str] = {}
    for source, target in raw.items():
        if not isinstance(source, str) or not isinstance(target, str):
            fail(
                E_CONFIG_INVALID,
                f"{key} keys and values must be strings",
                field=key,
                reason="invalid_field",
            )
        if not is_valid_name(source) or not is_valid_name(target):
            fail(
                E_CONFIG_INVALID,
                f"invalid type or alias name in {key}",
                field=f"{key}.{source}",
                reason="invalid_name",
                value=target,
            )
        result[source] = target
    return dict(sorted(result.items()))


def config_from_dict(data: dict[str, Any]) -> GovernanceConfig:
    if not isinstance(data, dict):
        fail(
            E_CONFIG_INVALID,
            "annotation config must be a JSON object",
            field="$",
            reason="invalid_root_type",
        )
    required = {
        "merge_strategy",
        "annotator_priority",
        "alias_mapping",
        "max_tokens_per_doc",
        "max_types",
        "max_resident_votes",
        "retirement_policy",
        "retirement_substitutes",
    }
    missing = sorted(required - set(data))
    if missing:
        fail(
            E_CONFIG_INVALID,
            f"missing required config field {missing[0]}",
            field=missing[0],
            reason="required_field_missing",
        )
    known = required | {
        "resource_policy",
        "label_set_version",
        "downgrade_policy",
    }
    unknown = sorted(set(data) - known)
    if unknown:
        fail(
            E_CONFIG_INVALID,
            f"unknown config field {unknown[0]!r}",
            field=unknown[0],
            reason="unknown_field",
        )
    strategy = data["merge_strategy"]
    if strategy not in ("majority", "annotator_priority"):
        fail(
            E_CONFIG_INVALID,
            "merge_strategy must be majority or annotator_priority",
            field="merge_strategy",
            reason="invalid_field",
            value=strategy,
        )
    priority = data["annotator_priority"]
    if (
        not isinstance(priority, list)
        or not priority
        or any(not isinstance(item, str) or not item for item in priority)
    ):
        fail(
            E_CONFIG_INVALID,
            "annotator_priority must be a non-empty string array",
            field="annotator_priority",
            reason="invalid_field",
        )
    if len(set(priority)) != len(priority):
        fail(
            E_CONFIG_INVALID,
            "annotator_priority contains duplicates",
            field="annotator_priority",
            reason="duplicate_value",
        )
    retirement_policy = data["retirement_policy"]
    if retirement_policy not in ("substitute", "reject", "doc_reject"):
        fail(
            E_CONFIG_INVALID,
            "retirement_policy must be substitute, reject or doc_reject",
            field="retirement_policy",
            reason="invalid_field",
            value=retirement_policy,
        )
    resource_policy = data.get("resource_policy", "fail")
    if resource_policy not in ("fail", "degrade"):
        fail(
            E_CONFIG_INVALID,
            "resource_policy must be fail or degrade",
            field="resource_policy",
            reason="invalid_field",
            value=resource_policy,
        )
    label_version = data.get("label_set_version")
    if label_version is not None and (
        not isinstance(label_version, str) or not label_version
    ):
        fail(
            E_CONFIG_INVALID,
            "label_set_version must be a non-empty string when provided",
            field="label_set_version",
            reason="invalid_field",
        )
    downgrade_raw = data.get(
        "downgrade_policy",
        {
            "nested_aliases": "flatten",
            "disabled_aliases": "discard",
            "bio_prefix_required": "flatten",
        },
    )
    if not isinstance(downgrade_raw, dict):
        fail(
            E_CONFIG_INVALID,
            "downgrade_policy must be an object",
            field="downgrade_policy",
            reason="invalid_field",
        )
    downgrade = {
        "nested_aliases": "flatten",
        "disabled_aliases": "discard",
        "bio_prefix_required": "flatten",
    }
    allowed_downgrade = {
        "nested_aliases": {"flatten", "discard"},
        "disabled_aliases": {"flatten", "discard"},
        "bio_prefix_required": {"flatten", "reject"},
    }
    for key, raw_value in downgrade_raw.items():
        if key not in allowed_downgrade:
            fail(
                E_CONFIG_INVALID,
                f"unknown downgrade policy {key!r}",
                field=f"downgrade_policy.{key}",
                reason="unknown_field",
            )
        if raw_value not in allowed_downgrade[key]:
            fail(
                E_CONFIG_INVALID,
                f"invalid downgrade policy for {key}",
                field=f"downgrade_policy.{key}",
                reason="invalid_field",
                value=raw_value,
            )
        downgrade[key] = raw_value
    return GovernanceConfig(
        merge_strategy=strategy,
        annotator_priority=list(priority),
        alias_mapping=_string_map(data, "alias_mapping", required=True),
        max_tokens_per_doc=_require_int(data, "max_tokens_per_doc"),
        max_types=_require_int(data, "max_types"),
        max_resident_votes=_require_int(data, "max_resident_votes"),
        retirement_policy=retirement_policy,
        retirement_substitutes=_string_map(
            data,
            "retirement_substitutes",
            required=True,
        ),
        resource_policy=resource_policy,
        label_set_version=label_version,
        downgrade_policy=downgrade,
    )


def load_config(path: str | Path) -> GovernanceConfig:
    try:
        raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        fail(
            E_CONFIG_INVALID,
            f"cannot parse config: {exc}",
            field="path",
            reason="read_or_json_error",
            source=str(path),
        )
    return config_from_dict(data)


def merged_alias_map(
    type_set_aliases: dict[str, str],
    config_aliases: dict[str, str],
) -> dict[str, str]:
    """Combine type-set and config aliases, rejecting ambiguous targets."""

    result = dict(type_set_aliases)
    for alias, target in sorted(config_aliases.items()):
        existing = result.get(alias)
        if existing is not None and existing != target:
            fail(
                E_CONFIG_INVALID,
                f"alias {alias!r} has conflicting targets",
                field=f"alias_mapping.{alias}",
                reason="conflicting_alias_target",
                existing=existing,
                configured=target,
            )
        result[alias] = target
    return dict(sorted(result.items()))


def config_to_json(config: GovernanceConfig) -> str:
    return canonical_json(config.to_payload())
