"""Stage 2: deterministic template expansion and canonicalization."""

from __future__ import annotations

import string
from typing import Any

from ..errors import (
    BusinessError,
    E_TEMPLATE_NOT_FOUND,
    E_TEMPLATE_PLACEHOLDER,
    E_WORKER_FAILURE,
)
from ..scheduler import run_worker_batches, select_worker_count
from ..types import BudgetLimits
from ..utils import normalize_text


def _template_for(
    sample: dict[str, Any],
    templates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for template in templates:
        if template.get("lang") != sample["lang"]:
            continue
        tags = template.get("source_tags")
        if tags and sample["source_tag"] not in tags:
            continue
        return template
    return None


def _parse_placeholders(
    template_text: str,
    *,
    template_id: str,
    allowed: set[str],
) -> list[str]:
    try:
        parsed = list(string.Formatter().parse(template_text))
    except ValueError as exc:
        raise BusinessError(
            E_TEMPLATE_PLACEHOLDER,
            f"template {template_id} contains malformed placeholder: {exc}",
            details={"template_id": template_id},
        ) from exc
    names: list[str] = []
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name or format_spec or conversion:
            raise BusinessError(
                E_TEMPLATE_PLACEHOLDER,
                (
                    f"template {template_id} uses unsupported placeholder "
                    f"{field_name!r}"
                ),
                details={
                    "template_id": template_id,
                    "field_name": field_name,
                    "format_spec": format_spec,
                    "conversion": conversion,
                },
            )
        if field_name not in allowed:
            raise BusinessError(
                E_TEMPLATE_PLACEHOLDER,
                f"template {template_id} references missing placeholder {field_name}",
                details={
                    "template_id": template_id,
                    "missing_placeholder": field_name,
                    "allowed_placeholders": sorted(allowed),
                },
            )
        names.append(field_name)
    return names


def _expand_sample(
    sample: dict[str, Any],
    templates: list[dict[str, Any]],
    template_config: dict[str, Any],
) -> dict[str, Any]:
    template = _template_for(sample, templates)
    if template is None:
        raise BusinessError(
            E_TEMPLATE_NOT_FOUND,
            "no template matches sample lang/source_tag",
            details={
                "sample_id": sample["sample_id"],
                "lang": sample["lang"],
                "source_tag": sample["source_tag"],
            },
        )
    template_id = template.get("template_id")
    if not template_id:
        raise BusinessError(
            E_TEMPLATE_PLACEHOLDER,
            "matched template has no template_id",
            details={"sample_id": sample["sample_id"]},
        )
    allowed = set(template_config.get("allowed_placeholders", []))
    required = template_config.get("required_placeholders", {})
    for key in ("instruction_template", "input_template"):
        if key not in template or not isinstance(template[key], str):
            raise BusinessError(
                E_TEMPLATE_PLACEHOLDER,
                f"template {template_id} is missing {key}",
                details={"template_id": template_id, "field": key},
            )
        names = _parse_placeholders(
            template[key],
            template_id=template_id,
            allowed=allowed,
        )
        for placeholder in required.get(key, []):
            if placeholder not in names:
                raise BusinessError(
                    E_TEMPLATE_PLACEHOLDER,
                    (
                        f"template {template_id} is missing required "
                        f"placeholder {{{placeholder}}} in {key}"
                    ),
                    details={
                        "template_id": template_id,
                        "field": key,
                        "missing_placeholder": placeholder,
                    },
                )
    scope = dict(sample)
    instruction = normalize_text(
        template["instruction_template"].format(**scope)
    )
    input_text = normalize_text(template["input_template"].format(**scope))
    canonical_text = normalize_text(
        f"{instruction}\n{input_text}" if input_text else instruction
    )
    expanded = dict(sample)
    expanded.update(
        {
            "template_id": template_id,
            "normalized_instruction": instruction,
            "normalized_input": input_text,
            "canonical_text": canonical_text,
        }
    )
    return expanded


def run_template(
    samples: list[dict[str, Any]],
    templates_doc: dict[str, Any],
    config: dict[str, Any],
    *,
    failpoints: dict[str, str] | None = None,
) -> dict[str, Any]:
    templates = templates_doc.get("templates", [])
    runtime = config["runtime"]
    limits = BudgetLimits(
        max_workers=int(runtime["max_workers"]),
        compare_budget=int(runtime["compare_budget"]),
        memory_limit_bytes=int(runtime["memory_limit_bytes"]),
        error_priority=tuple(runtime["budget_error_priority"]),
    )
    workers, worker_decisions = select_worker_count(
        requested_workers=int(runtime["requested_workers"]),
        limits=limits,
        base_memory_bytes=len(samples) * int(runtime["per_sample_memory_bytes"]),
        per_worker_memory_bytes=int(runtime["per_worker_memory_bytes"]),
        stage="template",
    )

    def worker(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for sample in batch:
            try:
                output.append(
                    _expand_sample(sample, templates, config["template"])
                )
            except BusinessError as exc:
                exc.details.setdefault("sample_id", sample.get("sample_id"))
                raise
        return output

    failures: dict[str, dict[str, Any]] = {}
    expanded: list[dict[str, Any]] = []
    # Template-level business errors are per-sample statuses; batch-level
    # unexpected worker errors still abort with attribution.
    try:
        expanded = run_worker_batches(
            samples,
            worker,
            stage="template",
            batch_size=int(runtime["batch_size"]),
            workers=workers,
            failpoints=failpoints,
        )
    except BusinessError as exc:
        if exc.code not in {
            E_TEMPLATE_NOT_FOUND,
            E_TEMPLATE_PLACEHOLDER,
        }:
            raise
        # Re-run deterministically to build complete per-sample statuses after
        # the first failing batch has already supplied worker attribution.
        for sample in samples:
            try:
                expanded.append(
                    _expand_sample(sample, templates, config["template"])
                )
            except BusinessError as item_error:
                failures[sample["sample_id"]] = item_error.to_dict()

    expanded.sort(key=lambda item: item["sample_id"])
    if not failures and len(expanded) != len(samples):
        # Defensive check for any missed business failure.
        seen = {item["sample_id"] for item in expanded}
        for sample in samples:
            if sample["sample_id"] not in seen:
                failures[sample["sample_id"]] = {
                    "code": E_WORKER_FAILURE,
                    "message": "sample was not processed",
                    "stage": "template",
                    "batch_id": None,
                    "details": {},
                }
    statuses = []
    for sample in samples:
        sample_id = sample["sample_id"]
        if sample_id in failures:
            statuses.append(
                {
                    "sample_id": sample_id,
                    "status": "error",
                    **failures[sample_id],
                }
            )
        else:
            statuses.append(
                {"sample_id": sample_id, "status": "ok", "code": None}
            )
    return {
        "stage": "template",
        "samples": expanded,
        "statuses": statuses,
        "summary": {
            "input_samples": len(samples),
            "output_samples": len(expanded),
            "failed_samples": len(failures),
        },
        "parallel": {
            "requested_workers": int(runtime["requested_workers"]),
            "workers": workers,
            "batch_size": int(runtime["batch_size"]),
            "worker_decisions": worker_decisions,
        },
        "completed_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
        "valid_sample_ids": [item["sample_id"] for item in expanded],
        "expected_sample_ids": sorted(
            sample["sample_id"] for sample in samples
        ),
    }

