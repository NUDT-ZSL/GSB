from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Callable

from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_INPUT_INVALID,
    E_TEMPLATE_INVALID,
)
from .storage import SnapshotStore
from .util import (
    atomic_write_bytes,
    canonical_json_text,
    read_json_file,
)
from .validation import (
    validate_cleaning_rules,
    validate_pipeline_config,
    validate_quotas,
    validate_sample_records,
    validate_templates,
)
from .workspace import Workspace


def _pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _load(path: Path, field: str) -> dict[str, Any]:
    try:
        value = read_json_file(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"cannot read {path.name}: {exc}",
            field=field,
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            E_CONFIG_INVALID, f"{path.name} must contain an object", field=field
        )
    return value


def _with_operation_lock(
    workspace: Workspace, operation: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    store = SnapshotStore(workspace)
    with store.operation_lock:
        return operation()


def edit_sample_output(
    workspace: Workspace, sample_id: str, output: str
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        pipeline = _load(workspace.pipeline_config_path, "pipeline_config")
        validate_pipeline_config(pipeline)
        try:
            raw_lines = workspace.samples_path.read_text(encoding="utf-8").splitlines()
            raw_records = [
                json.loads(line) for line in raw_lines if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_INPUT_INVALID,
                f"cannot read samples: {exc}",
                field="samples",
            ) from exc
        found = False
        previous_output: str | None = None
        for record in raw_records:
            if record.get("sample_id") == sample_id:
                found = True
                previous_output = record.get("output")
                record["output"] = output
                break
        if not found:
            raise BusinessError(
                E_INPUT_INVALID,
                "sample_id was not found",
                field="sample_id",
                sample_id=sample_id,
            )
        if not isinstance(output, str) or not output:
            raise BusinessError(
                E_INPUT_INVALID,
                "new output must be a non-empty string",
                field="output",
                sample_id=sample_id,
            )
        normalized = validate_sample_records(raw_records, pipeline)
        if previous_output == output:
            return {
                "ok": True,
                "changed": False,
                "edit": "sample_output",
                "sample_id": sample_id,
            }
        payload = "".join(
            canonical_json_text(record) + "\n" for record in normalized
        ).encode("utf-8")
        atomic_write_bytes(workspace.samples_path, payload)
        return {
            "ok": True,
            "changed": True,
            "edit": "sample_output",
            "sample_id": sample_id,
            "old_output_sha256": hashlib.sha256(
                (previous_output or "").encode("utf-8")
            ).hexdigest(),
            "new_output_sha256": hashlib.sha256(
                output.encode("utf-8")
            ).hexdigest(),
        }

    return _with_operation_lock(workspace, operation)


def edit_quality_weight(
    workspace: Workspace, weight_name: str, value: float
) -> dict[str, Any]:
    if weight_name not in {"length", "repetition", "noise"}:
        raise BusinessError(
            E_CONFIG_INVALID,
            "unknown quality weight",
            field=f"quality.weights.{weight_name}",
        )

    def operation() -> dict[str, Any]:
        rules = _load(workspace.rules_path, "cleaning_rules")
        quality = rules.setdefault("quality", {})
        weights = quality.setdefault("weights", {})
        old_value = weights.get(weight_name)
        weights[weight_name] = value
        validate_cleaning_rules(rules)
        atomic_write_bytes(workspace.rules_path, _pretty_json_bytes(rules))
        return {
            "ok": True,
            "changed": old_value != value,
            "edit": "quality_weight",
            "field": f"quality.weights.{weight_name}",
            "old_value": old_value,
            "new_value": value,
        }

    return _with_operation_lock(workspace, operation)


def add_template(workspace: Workspace, template: dict[str, Any]) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        library = _load(workspace.templates_path, "templates")
        templates = library.setdefault("templates", [])
        if not isinstance(templates, list):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "templates must be a list",
                field="templates",
            )
        templates.append(template)
        validate_templates(library)
        atomic_write_bytes(workspace.templates_path, _pretty_json_bytes(library))
        return {
            "ok": True,
            "changed": True,
            "edit": "add_template",
            "template_id": template.get("template_id"),
            "library_version": library.get("library_version"),
        }

    return _with_operation_lock(workspace, operation)


def remove_template(workspace: Workspace, template_id: str) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        library = _load(workspace.templates_path, "templates")
        templates = library.get("templates")
        if not isinstance(templates, list):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "templates must be a list",
                field="templates",
            )
        kept = [
            template
            for template in templates
            if not (
                isinstance(template, dict)
                and template.get("template_id") == template_id
            )
        ]
        if len(kept) == len(templates):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "template_id was not found",
                field="templates",
                details={"template_id": template_id},
            )
        library["templates"] = kept
        validate_templates(library)
        atomic_write_bytes(workspace.templates_path, _pretty_json_bytes(library))
        return {
            "ok": True,
            "changed": True,
            "edit": "remove_template",
            "template_id": template_id,
            "library_version": library.get("library_version"),
        }

    return _with_operation_lock(workspace, operation)


def set_quota_target(
    workspace: Workspace,
    source_tag: str,
    quality_tier: str,
    target: int,
) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        quotas = _load(workspace.quotas_path, "quotas")
        rules = _load(workspace.rules_path, "cleaning_rules")
        validate_cleaning_rules(rules)
        tiers = {tier["name"] for tier in rules["quality"]["tiers"]}
        if quality_tier not in tiers:
            raise BusinessError(
                E_CONFIG_INVALID,
                "quality_tier is not declared by cleaning rules",
                field="quality_tier",
                details={"quality_tier": quality_tier, "allowed": sorted(tiers)},
            )
        targets = quotas.get("targets")
        if not isinstance(targets, list):
            raise BusinessError(
                E_CONFIG_INVALID,
                "quotas.targets must be a list",
                field="quotas.targets",
            )
        old_value: int | None = None
        found = False
        for item in targets:
            if (
                isinstance(item, dict)
                and item.get("source_tag") == source_tag
                and item.get("quality_tier") == quality_tier
            ):
                old_value = item.get("target")
                item["target"] = target
                found = True
                break
        if not found:
            targets.append(
                {
                    "source_tag": source_tag,
                    "quality_tier": quality_tier,
                    "target": target,
                }
            )
        validate_quotas(quotas)
        atomic_write_bytes(workspace.quotas_path, _pretty_json_bytes(quotas))
        return {
            "ok": True,
            "changed": old_value != target,
            "edit": "quota_target",
            "source_tag": source_tag,
            "quality_tier": quality_tier,
            "old_target": old_value,
            "new_target": target,
        }

    return _with_operation_lock(workspace, operation)


def set_total_target(workspace: Workspace, target: int) -> dict[str, Any]:
    def operation() -> dict[str, Any]:
        quotas = _load(workspace.quotas_path, "quotas")
        old_value = quotas.get("total_target")
        quotas["total_target"] = target
        validate_quotas(quotas)
        atomic_write_bytes(workspace.quotas_path, _pretty_json_bytes(quotas))
        return {
            "ok": True,
            "changed": old_value != target,
            "edit": "total_target",
            "old_target": old_value,
            "new_target": target,
        }

    return _with_operation_lock(workspace, operation)
