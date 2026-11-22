"""Loss-aware, replayable migration between v1, v2 and v3."""

from __future__ import annotations

import copy
import json
from typing import Any

from .config import GovernanceConfig
from .errors import (
    E_SEMANTIC_DRIFT,
    E_TYPE_SET_CORRUPT,
    E_VERSION_UNSUPPORTED,
    fail,
)
from .models import AliasDefinition, MigrationResult, TypeSet
from .typesets import FORMAT_V1, FORMAT_V2, FORMAT_V3, read_type_set_text


def render_type_set(type_set: TypeSet, target_format: str) -> str:
    if target_format == FORMAT_V1:
        lines = [f"#NER_TYPE_SET|v1|{type_set.label_set_version}"]
        for item in sorted(type_set.types, key=lambda value: value.name):
            if not item.bio_prefix_required:
                fail(
                    E_TYPE_SET_CORRUPT,
                    "v1 cannot represent bio_prefix_required=false",
                    field=f"types[{item.name}].bio_prefix_required",
                    line=item.source_line,
                    reason="v1_unsupported_semantics",
                )
            enabled = "true" if item.enabled else "false"
            lines.append(f"TYPE|{item.name}|{enabled}")
        for item in sorted(type_set.types, key=lambda value: value.name):
            for alias in sorted(item.aliases, key=lambda value: value.name):
                if alias.metadata:
                    fail(
                        E_TYPE_SET_CORRUPT,
                        "v1 cannot represent alias metadata",
                        field=f"types[{item.name}].aliases[{alias.name}].metadata",
                        line=alias.source_line,
                        reason="v1_unsupported_semantics",
                    )
                if not alias.enabled:
                    fail(
                        E_TYPE_SET_CORRUPT,
                        "v1 cannot represent alias enabled=false",
                        field=f"types[{item.name}].aliases[{alias.name}].enabled",
                        line=alias.source_line,
                        reason="v1_unsupported_semantics",
                    )
                lines.append(f"ALIAS|{alias.name}|{alias.target}")
        return "\n".join(lines) + "\n"

    if target_format == FORMAT_V2:
        lines = [f"#NER_TYPE_SET|v2|{type_set.label_set_version}"]
        for item in sorted(type_set.types, key=lambda value: value.name):
            if item.aliases and any(alias.metadata for alias in item.aliases):
                alias = next(alias for alias in item.aliases if alias.metadata)
                fail(
                    E_TYPE_SET_CORRUPT,
                    "v2 cannot represent nested alias metadata",
                    field=f"types[{item.name}].aliases[{alias.name}].metadata",
                    line=alias.source_line,
                    reason="v2_unsupported_semantics",
                )
            lines.append(f"type={item.name}")
            lines.append(
                "bio_prefix_required="
                + ("true" if item.bio_prefix_required else "false")
            )
            lines.append("enabled=" + ("true" if item.enabled else "false"))
            for alias in sorted(item.aliases, key=lambda value: value.name):
                alias_enabled = "true" if alias.enabled else "false"
                lines.append(
                    f"alias={alias.name}>{alias.target}>{alias_enabled}"
                )
        return "\n".join(lines) + "\n"

    if target_format == FORMAT_V3:
        payload = {
            "format": "NER_TYPE_SET_V3",
            "label_set_version": type_set.label_set_version,
            "types": [
                {
                    "name": item.name,
                    "bio_prefix_required": item.bio_prefix_required,
                    "enabled": item.enabled,
                    "aliases": [
                        {
                            "name": alias.name,
                            "target": alias.target,
                            "enabled": alias.enabled,
                            "metadata": alias.metadata,
                        }
                        for alias in sorted(item.aliases, key=lambda value: value.name)
                    ],
                }
                for item in sorted(type_set.types, key=lambda value: value.name)
            ],
        }
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

    fail(
        E_VERSION_UNSUPPORTED,
        f"unsupported target format {target_format!r}",
        field="target_format",
        version=target_format,
    )
    raise AssertionError("unreachable")


def _drop(
    drops: list[dict[str, Any]],
    *,
    item: str,
    kind: str,
    action: str,
    reason: str,
    source_line: int | None,
    lossy: bool,
    **extra: Any,
) -> None:
    detail = {
        "item": item,
        "kind": kind,
        "action": action,
        "reason": reason,
        "source_line": source_line,
        "lossy": lossy,
    }
    detail.update(extra)
    drops.append(detail)


def migrate_type_set(
    source: TypeSet,
    target_format: str,
    config: GovernanceConfig,
) -> MigrationResult:
    """Migrate and immediately read back the rendered target format."""

    if target_format not in (FORMAT_V1, FORMAT_V2, FORMAT_V3):
        fail(
            E_VERSION_UNSUPPORTED,
            f"unsupported target format {target_format!r}",
            field="target_format",
            version=target_format,
        )
    types = copy.deepcopy(source.types)
    completions: list[dict[str, Any]] = []
    drops: list[dict[str, Any]] = []

    if target_format in (FORMAT_V2, FORMAT_V3):
        for record in source.completions:
            completion = dict(record)
            completion["target_format"] = target_format
            completions.append(completion)
        if target_format == FORMAT_V3 and source.format_version in (
            FORMAT_V1,
            FORMAT_V2,
        ):
            for owner in types:
                for alias in owner.aliases:
                    completions.append(
                        {
                            "item": (
                                f"types[{owner.name}].aliases[{alias.name}].metadata"
                            ),
                            "value": {},
                            "source": (
                                "v1_implicit_default"
                                if source.format_version == FORMAT_V1
                                else "v2_implicit_default"
                            ),
                            "source_line": alias.source_line,
                            "target_format": target_format,
                        }
                    )

    if target_format == FORMAT_V1 and source.format_version != FORMAT_V1:
        for owner in types:
            if not owner.bio_prefix_required:
                if config.downgrade_policy["bio_prefix_required"] == "reject":
                    fail(
                        E_SEMANTIC_DRIFT,
                        "v1 cannot preserve bio_prefix_required=false",
                        field=f"types[{owner.name}].bio_prefix_required",
                        line=owner.source_line,
                        reason="v1_unsupported_semantics",
                    )
                _drop(
                    drops,
                    item=f"types[{owner.name}].bio_prefix_required",
                    kind="bio_prefix_required",
                    action="flattened_to_default",
                    reason="v1_always_requires_bio_prefix",
                    source_line=owner.source_line,
                    lossy=True,
                    original_value=False,
                    migrated_value=True,
                )
                owner.bio_prefix_required = True
        for owner in types:
            retained: list[AliasDefinition] = []
            for alias in owner.aliases:
                item = f"types[{owner.name}].aliases[{alias.name}]"
                if alias.metadata:
                    if config.downgrade_policy["nested_aliases"] == "discard":
                        _drop(
                            drops,
                            item=item,
                            kind="nested_alias_metadata",
                            action="discarded_alias",
                            reason="v1_cannot_express_nested_alias_metadata",
                            source_line=alias.source_line,
                            lossy=True,
                            metadata=alias.metadata,
                        )
                        continue
                    _drop(
                        drops,
                        item=item,
                        kind="nested_alias_metadata",
                        action="flattened_metadata_dropped",
                        reason="v1_cannot_express_nested_alias_metadata",
                        source_line=alias.source_line,
                        lossy=True,
                        metadata=alias.metadata,
                    )
                    alias.metadata = {}
                if not alias.enabled:
                    if config.downgrade_policy["disabled_aliases"] == "discard":
                        _drop(
                            drops,
                            item=item,
                            kind="alias_enabled",
                            action="discarded_alias",
                            reason="v1_cannot_express_alias_enabled_marker",
                            source_line=alias.source_line,
                            lossy=True,
                            original_value=False,
                        )
                        continue
                    _drop(
                        drops,
                        item=item,
                        kind="alias_enabled",
                        action="flattened_to_default",
                        reason="v1_cannot_express_alias_enabled_marker",
                        source_line=alias.source_line,
                        lossy=True,
                        original_value=False,
                        migrated_value=True,
                    )
                    alias.enabled = True
                _drop(
                    drops,
                    item=item,
                    kind="nested_alias",
                    action="representation_flattened",
                    reason="v1_uses_flat_alias_rows",
                    source_line=alias.source_line,
                    lossy=False,
                )
                retained.append(alias)
            owner.aliases = retained
    elif target_format == FORMAT_V2:
        for owner in types:
            retained = []
            for alias in owner.aliases:
                item = f"types[{owner.name}].aliases[{alias.name}]"
                if alias.metadata:
                    if config.downgrade_policy["nested_aliases"] == "discard":
                        _drop(
                            drops,
                            item=item,
                            kind="nested_alias_metadata",
                            action="discarded_alias",
                            reason="v2_cannot_express_nested_alias_metadata",
                            source_line=alias.source_line,
                            lossy=True,
                            metadata=alias.metadata,
                        )
                        continue
                    _drop(
                        drops,
                        item=item,
                        kind="nested_alias_metadata",
                        action="flattened_metadata_dropped",
                        reason="v2_cannot_express_nested_alias_metadata",
                        source_line=alias.source_line,
                        lossy=True,
                        metadata=alias.metadata,
                    )
                    alias.metadata = {}
                retained.append(alias)
            owner.aliases = retained

    expected = TypeSet(
        format_version=target_format,
        label_set_version=source.label_set_version,
        types=types,
        source_name=source.source_name,
    )
    text = render_type_set(expected, target_format)
    replayed = read_type_set_text(
        text,
        source_name=f"replay:{source.source_name or '<memory>'}:{target_format}",
    )
    expected_fingerprint = expected.semantic_fingerprint()
    replay_fingerprint = replayed.semantic_fingerprint()
    replay_ok = replay_fingerprint == expected_fingerprint
    if not replay_ok:
        fail(
            E_SEMANTIC_DRIFT,
            "migrated type set did not replay to the expected semantics",
            field="migration",
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=replay_fingerprint,
        )
    source_fingerprint = source.semantic_fingerprint()
    return MigrationResult(
        source_format=source.format_version,
        target_format=target_format,
        source_fingerprint=source_fingerprint,
        target_fingerprint=expected_fingerprint,
        fingerprint_after_replay=replay_fingerprint,
        text=text,
        replay_ok=True,
        lossless=source_fingerprint == expected_fingerprint,
        completions=completions,
        drops=drops,
        target_type_set=replayed,
    )
