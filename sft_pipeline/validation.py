from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_CONFIG_INVALID,
    E_DUP_SAMPLE,
    E_INPUT_INVALID,
    E_LANG_INVALID,
    E_TEMPLATE_INVALID,
)
from .util import read_json_file, sha256_file, sha256_json
from .workspace import Workspace


REQUIRED_SAMPLE_FIELDS = (
    "sample_id",
    "instruction",
    "input",
    "output",
    "source_tag",
    "quality_flag",
    "lang",
    "annotation_revision",
)

PLACEHOLDER_RE = re.compile(r"\{(instruction|input|output)\}")
ANY_BRACE_NAME_RE = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True)
class ConfigBundle:
    pipeline: dict[str, Any]
    templates: dict[str, Any]
    rules: dict[str, Any]
    quotas: dict[str, Any]
    samples: list[dict[str, Any]]
    samples_digest: str

    @property
    def ingest_scope_digest(self) -> str:
        return sha256_json(
            {
                "schema_version": self.pipeline.get("schema_version"),
                "allowed_langs": self.pipeline["allowed_langs"],
                "required_fields": self.pipeline["required_fields"],
                "annotation_revision_pattern": self.pipeline[
                    "annotation_revision_pattern"
                ],
            }
        )

    @property
    def templates_digest(self) -> str:
        return sha256_json(self.templates)

    @property
    def quality_scope_digest(self) -> str:
        return sha256_json(self.rules["quality"])

    @property
    def dedupe_scope_digest(self) -> str:
        return sha256_json(self.rules["dedupe"])

    @property
    def quotas_digest(self) -> str:
        return sha256_json(self.quotas)

    @property
    def export_scope_digest(self) -> str:
        return sha256_json(
            {
                "export": self.pipeline["export"],
                "export_schema": 1,
            }
        )


def _config_error(field: str, message: str, **details: Any) -> BusinessError:
    return BusinessError(
        E_CONFIG_INVALID,
        message,
        field=field,
        details=details,
    )


def _load_json_config(path: Path, label: str) -> dict[str, Any]:
    try:
        value = read_json_file(path)
    except FileNotFoundError as exc:
        raise _config_error(label, f"missing configuration file: {path.name}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise _config_error(label, f"cannot parse {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise _config_error(label, f"{path.name} must contain a JSON object")
    return value


def validate_pipeline_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise _config_error("pipeline_config", "pipeline configuration must be an object")
    if config.get("schema_version") != 1:
        raise _config_error("schema_version", "schema_version must be 1")

    langs = config.get("allowed_langs")
    if (
        not isinstance(langs, list)
        or not langs
        or any(not isinstance(lang, str) or not lang for lang in langs)
        or len(set(langs)) != len(langs)
    ):
        raise _config_error(
            "allowed_langs", "allowed_langs must be a non-empty unique string list"
        )

    required = config.get("required_fields")
    if not isinstance(required, list) or any(
        not isinstance(field, str) for field in required
    ):
        raise _config_error(
            "required_fields", "required_fields must be a string list"
        )
    missing = sorted(set(REQUIRED_SAMPLE_FIELDS) - set(required))
    if missing:
        raise _config_error(
            "required_fields",
            "required_fields omits mandatory sample fields",
            missing=missing,
        )

    pattern = config.get("annotation_revision_pattern")
    if not isinstance(pattern, str):
        raise _config_error(
            "annotation_revision_pattern",
            "annotation_revision_pattern must be a string",
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise _config_error(
            "annotation_revision_pattern",
            f"invalid annotation revision regex: {exc}",
        ) from exc

    export = config.get("export")
    if not isinstance(export, dict):
        raise _config_error("export", "export must be an object")
    for field in ("dataset_name", "format_version"):
        if not isinstance(export.get(field), str) or not export[field]:
            raise _config_error(f"export.{field}", f"export.{field} is required")
    if export.get("format") != "jsonl":
        raise _config_error("export.format", "only jsonl export is supported")

    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise _config_error("limits", "limits must be an object")
    positive_fields = {
        "max_samples_per_stage": int,
        "max_resident_memory_mb": (int, float),
        "max_parallel_stages": int,
        "requested_parallel_stages": int,
    }
    for field, expected_type in positive_fields.items():
        value = limits.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, expected_type)
            or value <= 0
        ):
            raise _config_error(
                f"limits.{field}", f"limits.{field} must be positive"
            )
    if limits["requested_parallel_stages"] < 1:
        raise _config_error(
            "limits.requested_parallel_stages",
            "requested_parallel_stages must be at least 1",
        )


def validate_templates(library: dict[str, Any]) -> None:
    if not isinstance(library, dict):
        raise BusinessError(
            E_TEMPLATE_INVALID,
            "template library must be an object",
            field="templates",
        )
    version = library.get("library_version")
    if not isinstance(version, str) or not version:
        raise BusinessError(
            E_TEMPLATE_INVALID,
            "library_version is required",
            field="library_version",
        )
    selection = library.get("selection")
    if (
        not isinstance(selection, dict)
        or selection.get("mode") != "stable_hash"
        or not isinstance(selection.get("salt"), str)
        or not selection["salt"]
    ):
        raise BusinessError(
            E_TEMPLATE_INVALID,
            "selection must use stable_hash with a non-empty salt",
            field="selection",
        )
    templates = library.get("templates")
    if not isinstance(templates, list) or not templates:
        raise BusinessError(
            E_TEMPLATE_INVALID,
            "templates must be a non-empty list",
            field="templates",
        )

    seen: set[str] = set()
    for index, template in enumerate(templates):
        prefix = f"templates[{index}]"
        if not isinstance(template, dict):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "template entry must be an object",
                field=prefix,
            )
        template_id = template.get("template_id")
        if not isinstance(template_id, str) or not template_id:
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "template_id is required",
                field=f"{prefix}.template_id",
            )
        if template_id in seen:
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "duplicate template_id",
                field=f"{prefix}.template_id",
                details={"template_id": template_id},
            )
        seen.add(template_id)
        source_tags = template.get("source_tags")
        if not isinstance(source_tags, list) or any(
            not isinstance(tag, str) or not tag for tag in source_tags
        ):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "source_tags must be a string list",
                field=f"{prefix}.source_tags",
            )
        pattern = template.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "pattern is required",
                field=f"{prefix}.pattern",
            )
        placeholders = set(PLACEHOLDER_RE.findall(pattern))
        missing = {"instruction", "input", "output"} - placeholders
        without_known = PLACEHOLDER_RE.sub("", pattern)
        has_unknown_brace = bool(ANY_BRACE_NAME_RE.search(without_known)) or any(
            char in without_known for char in "{}"
        )
        if missing:
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "template pattern is missing required placeholders",
                field=f"{prefix}.pattern",
                details={"missing": sorted(missing), "template_id": template_id},
            )
        if has_unknown_brace:
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "template pattern contains an unsupported placeholder",
                field=f"{prefix}.pattern",
                details={"template_id": template_id},
            )
        enabled = template.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BusinessError(
                E_TEMPLATE_INVALID,
                "enabled must be boolean",
                field=f"{prefix}.enabled",
            )


def validate_cleaning_rules(rules: dict[str, Any]) -> None:
    if not isinstance(rules, dict):
        raise _config_error("cleaning_rules", "cleaning rules must be an object")
    quality = rules.get("quality")
    if not isinstance(quality, dict):
        raise _config_error("quality", "quality rules must be an object")
    if not isinstance(quality.get("version"), str) or not quality["version"]:
        raise _config_error("quality.version", "quality.version is required")
    length_target = quality.get("length_target")
    if (
        isinstance(length_target, bool)
        or not isinstance(length_target, int)
        or length_target <= 0
    ):
        raise _config_error(
            "quality.length_target", "quality.length_target must be positive"
        )
    ngram_size = quality.get("ngram_size")
    if isinstance(ngram_size, bool) or not isinstance(ngram_size, int) or ngram_size <= 0:
        raise _config_error(
            "quality.ngram_size", "quality.ngram_size must be positive"
        )
    weights = quality.get("weights")
    if not isinstance(weights, dict):
        raise _config_error("quality.weights", "quality.weights must be an object")
    for name in ("length", "repetition", "noise"):
        value = weights.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise _config_error(
                f"quality.weights.{name}",
                f"quality weight {name} must be positive",
            )
    if sum(float(weights[name]) for name in ("length", "repetition", "noise")) <= 0:
        raise _config_error("quality.weights", "quality weight sum must be positive")
    noise_chars = quality.get("noise_chars")
    if not isinstance(noise_chars, str):
        raise _config_error(
            "quality.noise_chars", "quality.noise_chars must be a string"
        )
    tiers = quality.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        raise _config_error("quality.tiers", "quality.tiers must be non-empty")
    seen_tiers: set[str] = set()
    previous = 2.0
    for index, tier in enumerate(tiers):
        prefix = f"quality.tiers[{index}]"
        if not isinstance(tier, dict):
            raise _config_error(prefix, "quality tier must be an object")
        name = tier.get("name")
        minimum = tier.get("min_score")
        if not isinstance(name, str) or not name:
            raise _config_error(f"{prefix}.name", "tier name is required")
        if name in seen_tiers:
            raise _config_error(f"{prefix}.name", "tier name must be unique")
        seen_tiers.add(name)
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, (int, float))
            or minimum < 0
            or minimum > 1
        ):
            raise _config_error(
                f"{prefix}.min_score", "tier min_score must be within [0, 1]"
            )
        if minimum > previous:
            raise _config_error(
                f"{prefix}.min_score",
                "tier thresholds must be declared in descending order",
            )
        previous = float(minimum)
    if float(tiers[-1]["min_score"]) != 0.0:
        raise _config_error(
            "quality.tiers[-1].min_score",
            "the final quality tier must start at 0",
        )

    dedupe = rules.get("dedupe")
    if not isinstance(dedupe, dict):
        raise _config_error("dedupe", "dedupe rules must be an object")
    threshold = dedupe.get("threshold")
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not (0.0 < float(threshold) <= 1.0)
    ):
        raise _config_error(
            "dedupe.threshold", "dedupe.threshold must be in (0, 1]"
        )
    for field in ("shingle_size", "signature_size"):
        value = dedupe.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _config_error(f"dedupe.{field}", f"dedupe.{field} must be positive")


def validate_quotas(quotas: dict[str, Any]) -> None:
    if not isinstance(quotas, dict):
        raise _config_error("quotas", "quotas must be an object")
    if not isinstance(quotas.get("version"), str) or not quotas["version"]:
        raise _config_error("quotas.version", "quotas.version is required")
    seed = quotas.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise _config_error("quotas.seed", "quotas.seed must be an integer")
    total = quotas.get("total_target")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise _config_error(
            "quotas.total_target", "quotas.total_target must be positive"
        )
    targets = quotas.get("targets")
    if not isinstance(targets, list) or not targets:
        raise _config_error("quotas.targets", "quotas.targets must be non-empty")
    seen: set[tuple[str, str]] = set()
    for index, target in enumerate(targets):
        prefix = f"quotas.targets[{index}]"
        if not isinstance(target, dict):
            raise _config_error(prefix, "quota target must be an object")
        source_tag = target.get("source_tag")
        quality_tier = target.get("quality_tier")
        value = target.get("target")
        if not isinstance(source_tag, str) or not source_tag:
            raise _config_error(f"{prefix}.source_tag", "source_tag is required")
        if not isinstance(quality_tier, str) or not quality_tier:
            raise _config_error(
                f"{prefix}.quality_tier", "quality_tier is required"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _config_error(
                f"{prefix}.target", "quota target must be positive"
            )
        key = (source_tag, quality_tier)
        if key in seen:
            raise _config_error(
                f"{prefix}.source_tag",
                "duplicate source_tag/quality_tier quota target",
            )
        seen.add(key)


def _normalize_annotation_revision(
    raw_value: Any, pattern: re.Pattern[str], record_id: str | None, field: str
) -> int:
    if isinstance(raw_value, bool):
        raise BusinessError(
            E_INPUT_INVALID,
            "annotation_revision must be parseable",
            field=field,
            sample_id=record_id,
        )
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str) and pattern.fullmatch(raw_value):
        value = int(raw_value)
    else:
        raise BusinessError(
            E_INPUT_INVALID,
            "annotation_revision must be parseable",
            field=field,
            sample_id=record_id,
        )
    if value <= 0:
        raise BusinessError(
            E_INPUT_INVALID,
            "annotation_revision must be positive",
            field=field,
            sample_id=record_id,
        )
    return value


def validate_sample_records(
    records: list[dict[str, Any]],
    pipeline_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate raw records and return canonical, sample_id-sorted records."""
    allowed_langs = set(pipeline_config["allowed_langs"])
    required_fields = tuple(pipeline_config["required_fields"])
    revision_pattern = re.compile(pipeline_config["annotation_revision_pattern"])
    seen: dict[str, int] = {}
    normalized: list[dict[str, Any]] = []

    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise BusinessError(
                E_INPUT_INVALID,
                "sample record must be a JSON object",
                field="record",
                details={"line": index},
            )
        for field in required_fields:
            if field not in record:
                raise BusinessError(
                    E_INPUT_INVALID,
                    "required field is missing",
                    field=field,
                    sample_id=record.get("sample_id"),
                    details={"line": index},
                )
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise BusinessError(
                E_INPUT_INVALID,
                "sample_id must be a non-empty string",
                field="sample_id",
                details={"line": index},
            )
        if sample_id in seen:
            raise BusinessError(
                E_DUP_SAMPLE,
                "duplicate sample_id",
                field="sample_id",
                sample_id=sample_id,
                details={"first_line": seen[sample_id], "duplicate_line": index},
            )
        seen[sample_id] = index

        lang = record.get("lang")
        if not isinstance(lang, str) or lang not in allowed_langs:
            raise BusinessError(
                E_LANG_INVALID,
                "lang is not in the controlled language set",
                field="lang",
                sample_id=sample_id,
                details={"value": lang, "allowed": sorted(allowed_langs)},
            )
        for field in ("instruction", "input", "output", "source_tag", "quality_flag"):
            if not isinstance(record.get(field), str):
                raise BusinessError(
                    E_INPUT_INVALID,
                    f"{field} must be a string",
                    field=field,
                    sample_id=sample_id,
                )
        if not record["instruction"]:
            raise BusinessError(
                E_INPUT_INVALID,
                "instruction must not be empty",
                field="instruction",
                sample_id=sample_id,
            )
        if not record["output"]:
            raise BusinessError(
                E_INPUT_INVALID,
                "output must not be empty",
                field="output",
                sample_id=sample_id,
            )
        if not record["source_tag"]:
            raise BusinessError(
                E_INPUT_INVALID,
                "source_tag must not be empty",
                field="source_tag",
                sample_id=sample_id,
            )
        if not record["quality_flag"]:
            raise BusinessError(
                E_INPUT_INVALID,
                "quality_flag must not be empty",
                field="quality_flag",
                sample_id=sample_id,
            )
        revision = _normalize_annotation_revision(
            record["annotation_revision"],
            revision_pattern,
            sample_id,
            "annotation_revision",
        )
        canonical_record = {
            "sample_id": sample_id,
            "instruction": record["instruction"],
            "input": record["input"],
            "output": record["output"],
            "source_tag": record["source_tag"],
            "quality_flag": record["quality_flag"],
            "lang": record["lang"],
            "annotation_revision": revision,
        }
        normalized.append(canonical_record)

    if not normalized:
        raise BusinessError(
            E_INPUT_INVALID,
            "sample list must not be empty",
            field="samples",
        )
    normalized.sort(key=lambda item: item["sample_id"])
    return normalized


def validate_samples_file(path: Path, pipeline_config: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise BusinessError(
            E_INPUT_INVALID,
            f"cannot read sample file: {exc}",
            field="samples",
        ) from exc
    records: list[dict[str, Any]] = []
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BusinessError(
            E_INPUT_INVALID,
            "samples.jsonl must be UTF-8",
            field="samples",
        ) from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                E_INPUT_INVALID,
                "invalid JSONL sample line",
                field="samples",
                details={"line": line_number, "error": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                E_INPUT_INVALID,
                "sample line must be a JSON object",
                field="samples",
                details={"line": line_number},
            )
        value["__line__"] = line_number
        records.append(value)
    for record in records:
        record.pop("__line__", None)
    normalized = validate_sample_records(records, pipeline_config)
    return normalized, sha256_file(path)


def load_bundle(workspace: Workspace) -> ConfigBundle:
    pipeline = _load_json_config(workspace.pipeline_config_path, "pipeline_config")
    templates = _load_json_config(workspace.templates_path, "templates")
    rules = _load_json_config(workspace.rules_path, "cleaning_rules")
    quotas = _load_json_config(workspace.quotas_path, "quotas")
    validate_pipeline_config(pipeline)
    validate_templates(templates)
    validate_cleaning_rules(rules)
    validate_quotas(quotas)
    quality_tiers = {tier["name"] for tier in rules["quality"]["tiers"]}
    for index, target in enumerate(quotas["targets"]):
        if target["quality_tier"] not in quality_tiers:
            raise _config_error(
                f"quotas.targets[{index}].quality_tier",
                "quota quality_tier is not declared by cleaning rules",
                quality_tier=target["quality_tier"],
            )
    samples, digest = validate_samples_file(workspace.samples_path, pipeline)
    return ConfigBundle(
        pipeline=pipeline,
        templates=templates,
        rules=rules,
        quotas=quotas,
        samples=samples,
        samples_digest=digest,
    )
