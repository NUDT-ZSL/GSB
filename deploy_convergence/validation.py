"""Manifest and parameter validation helpers."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from typing import Any

from .canonical import is_int, is_number
from .errors import EngineError


SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CORE_PARAMETERS = (
    "image_tag",
    "replicas",
    "timeout_ms",
    "retry_factor",
    "resource_limit",
)
RESOURCE_FIELDS = ("cpu_millicores", "memory_mb")


def fail(
    code: str,
    message: str,
    *,
    stage: str,
    **details: Any,
) -> None:
    raise EngineError(code, message, stage=stage, details=details)


def validate_service_name(name: Any, *, stage: str, location: dict[str, Any]) -> str:
    if not isinstance(name, str) or not SERVICE_NAME_RE.fullmatch(name):
        fail(
            "E_BAD_MANIFEST",
            "service name must match ^[a-z0-9][a-z0-9-]*$",
            stage=stage,
            service=name,
            location=location,
        )
    return name


def validate_positive_int(
    value: Any,
    field: str,
    *,
    stage: str,
    location: dict[str, Any],
) -> int:
    if not is_int(value) or value <= 0:
        fail(
            "E_BAD_MANIFEST",
            f"{field} must be a positive integer",
            stage=stage,
            field=field,
            value=value,
            location=location,
        )
    return value


def validate_nonnegative_int(
    value: Any,
    field: str,
    *,
    stage: str,
    location: dict[str, Any],
) -> int:
    if not is_int(value) or value < 0:
        fail(
            "E_BAD_MANIFEST",
            f"{field} must be a non-negative integer",
            stage=stage,
            field=field,
            value=value,
            location=location,
        )
    return value


def validate_retry_factor(
    value: Any,
    *,
    stage: str,
    location: dict[str, Any],
) -> int | float:
    if not is_number(value) or Decimal(str(value)) <= 0:
        fail(
            "E_BAD_MANIFEST",
            "retry_factor must be a finite positive number",
            stage=stage,
            field="retry_factor",
            value=value,
            location=location,
        )
    return value


def validate_resource_limit(
    value: Any,
    *,
    stage: str,
    location: dict[str, Any],
) -> dict[str, int]:
    if not isinstance(value, dict):
        fail(
            "E_BAD_PARAM_TYPE",
            "resource_limit must be an object",
            stage=stage,
            field="resource_limit",
            value=value,
            location=location,
        )
    unknown = sorted(set(value) - set(RESOURCE_FIELDS))
    missing = sorted(set(RESOURCE_FIELDS) - set(value))
    if unknown or missing:
        fail(
            "E_BAD_PARAM_TYPE",
            "resource_limit fields do not match the declared object type",
            stage=stage,
            field="resource_limit",
            missing=missing,
            unknown=unknown,
            location=location,
        )
    return {
        field: validate_positive_int(
            value[field],
            f"resource_limit.{field}",
            stage=stage,
            location=location,
        )
        for field in RESOURCE_FIELDS
    }


def validate_parameter_type_schema(
    schema: Any,
    *,
    stage: str,
    source: str,
) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        fail(
            "E_BAD_MANIFEST",
            "parameter type declarations must be a non-empty object",
            stage=stage,
            source=source,
        )
    for key, spec in schema.items():
        if not isinstance(key, str) or not key:
            fail(
                "E_BAD_MANIFEST",
                "parameter names must be non-empty strings",
                stage=stage,
                source=source,
                parameter=key,
            )
        if isinstance(spec, str):
            if spec not in {"integer", "number", "string", "boolean"}:
                fail(
                    "E_BAD_MANIFEST",
                    "unsupported parameter type declaration",
                    stage=stage,
                    source=source,
                    parameter=key,
                    type=spec,
                )
        elif isinstance(spec, dict):
            if spec.get("type") != "object" or not isinstance(spec.get("fields"), dict):
                fail(
                    "E_BAD_MANIFEST",
                    "object parameter declaration must contain type=object and fields",
                    stage=stage,
                    source=source,
                    parameter=key,
                )
            for field, field_type in spec["fields"].items():
                if field_type not in {"integer", "number", "string", "boolean"}:
                    fail(
                        "E_BAD_MANIFEST",
                        "unsupported object field type declaration",
                        stage=stage,
                        source=source,
                        parameter=key,
                        field=field,
                        type=field_type,
                    )
        else:
            fail(
                "E_BAD_MANIFEST",
                "parameter type declaration must be a string or object",
                stage=stage,
                source=source,
                parameter=key,
            )
    for required in CORE_PARAMETERS:
        if required not in schema:
            fail(
                "E_BAD_MANIFEST",
                "parameter schema is missing a required deployment parameter",
                stage=stage,
                source=source,
                parameter=required,
            )
    return schema


def _matches_scalar(value: Any, type_name: str) -> bool:
    if type_name == "integer":
        return is_int(value)
    if type_name == "number":
        return is_number(value)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    return False


def validate_parameter_value(
    parameter: str,
    value: Any,
    spec: Any,
    *,
    stage: str,
    location: dict[str, Any],
) -> None:
    if isinstance(spec, str):
        if not _matches_scalar(value, spec):
            fail(
                "E_BAD_PARAM_TYPE",
                "parameter value type does not match declaration",
                stage=stage,
                parameter=parameter,
                expected=spec,
                value=value,
                location=location,
            )
        return
    if not isinstance(value, dict):
        fail(
            "E_BAD_PARAM_TYPE",
            "object parameter value type does not match declaration",
            stage=stage,
            parameter=parameter,
            expected="object",
            value=value,
            location=location,
        )
    fields = spec["fields"]
    unknown = sorted(set(value) - set(fields))
    missing = sorted(set(fields) - set(value))
    if unknown or missing:
        fail(
            "E_BAD_PARAM_TYPE",
            "object parameter fields do not match declaration",
            stage=stage,
            parameter=parameter,
            missing=missing,
            unknown=unknown,
            location=location,
        )
    for field in sorted(fields):
        if not _matches_scalar(value[field], fields[field]):
            fail(
                "E_BAD_PARAM_TYPE",
                "object parameter field type does not match declaration",
                stage=stage,
                parameter=parameter,
                field=field,
                expected=fields[field],
                value=value[field],
                location=location,
            )


def validate_params(
    params: Any,
    schema: dict[str, Any],
    *,
    stage: str,
    location: dict[str, Any],
    required: tuple[str, ...] = CORE_PARAMETERS,
) -> dict[str, Any]:
    if not isinstance(params, dict):
        fail(
            "E_BAD_MANIFEST",
            "params must be a flat object",
            stage=stage,
            location=location,
            value=params,
        )
    missing = sorted(set(required) - set(params))
    unknown = sorted(set(params) - set(schema))
    if missing or unknown:
        fail(
            "E_BAD_MANIFEST",
            "params do not match the declared parameter set",
            stage=stage,
            location=location,
            missing=missing,
            unknown=unknown,
        )
    for key in sorted(params):
        validate_parameter_value(
            key,
            params[key],
            schema[key],
            stage=stage,
            location=location,
        )
    validate_positive_int(
        params["replicas"],
        "replicas",
        stage=stage,
        location=location,
    )
    validate_positive_int(
        params["timeout_ms"],
        "timeout_ms",
        stage=stage,
        location=location,
    )
    validate_retry_factor(
        params["retry_factor"],
        stage=stage,
        location=location,
    )
    validate_resource_limit(
        params["resource_limit"],
        stage=stage,
        location=location,
    )
    if not isinstance(params["image_tag"], str) or not params["image_tag"].strip():
        fail(
            "E_BAD_MANIFEST",
            "image_tag must be a non-empty string",
            stage=stage,
            location=location,
        )
    if "max_burst" in params:
        value = params["max_burst"]
        if not is_int(value) or value < 0:
            fail(
                "E_BAD_PARAM_TYPE",
                "max_burst must be a non-negative integer",
                stage=stage,
                parameter="max_burst",
                value=value,
                location=location,
            )
    return params


def validate_manifest_version(version: Any, *, stage: str, source: str) -> str:
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail(
            "E_BAD_MANIFEST",
            "manifest_version must be a semantic version string",
            stage=stage,
            source=source,
            value=version,
        )
    return version
