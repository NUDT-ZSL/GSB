"""Protection-mask normalization and precision-bound diagnostics."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .constants import (
    KNOWN_PROT_BITS,
    PERMISSION_BITS,
    PROT_EXEC,
    PROT_GUARD,
    PROT_NOCACHE,
    PROT_READ,
    PROT_WRITE,
)


NAME_TO_BIT: Dict[str, int] = {
    "none": 0,
    "0": 0,
    "-": 0,
    "---": 0,
    "read": PROT_READ,
    "r": PROT_READ,
    "prot_read": PROT_READ,
    "page_readonly": PROT_READ,
    "readonly": PROT_READ,
    "page_readwrite": PROT_READ | PROT_WRITE,
    "readwrite": PROT_READ | PROT_WRITE,
    "page_writecopy": PROT_READ | PROT_WRITE,
    "writecopy": PROT_READ | PROT_WRITE,
    "write": PROT_WRITE,
    "w": PROT_WRITE,
    "prot_write": PROT_WRITE,
    "writeonly": PROT_WRITE,
    "execute": PROT_EXEC,
    "exec": PROT_EXEC,
    "x": PROT_EXEC,
    "prot_exec": PROT_EXEC,
    "page_execute": PROT_EXEC,
    "page_execute_readwrite": PROT_READ | PROT_WRITE | PROT_EXEC,
    "page_execute_writecopy": PROT_READ | PROT_WRITE | PROT_EXEC,
    "guard": PROT_GUARD,
    "prot_guard": PROT_GUARD,
    "page_guard": PROT_GUARD,
    "nocache": PROT_NOCACHE,
    "no_cache": PROT_NOCACHE,
    "prot_nocache": PROT_NOCACHE,
    "page_nocache": PROT_NOCACHE,
}

BIT_TO_NAMES = {
    PROT_READ: "read",
    PROT_WRITE: "write",
    PROT_EXEC: "execute",
    PROT_GUARD: "guard",
    PROT_NOCACHE: "nocache",
}


class ProtectionParseError(ValueError):
    pass


def mask_names(mask: int) -> List[str]:
    names = [name for bit, name in BIT_TO_NAMES.items() if mask & bit]
    return names or ["none"]


def format_mask(mask: int) -> str:
    chars = [
        "r" if mask & PROT_READ else "-",
        "w" if mask & PROT_WRITE else "-",
        "x" if mask & PROT_EXEC else "-",
    ]
    suffix = []
    if mask & PROT_GUARD:
        suffix.append("guard")
    if mask & PROT_NOCACHE:
        suffix.append("nocache")
    text = "".join(chars)
    if suffix:
        text += "+" + "+".join(suffix)
    return text


def _parse_named_token(token: str) -> int:
    normalized = token.strip().lower().replace("-", "_")
    compact = token.strip().lower()
    if compact and re.fullmatch(r"[rwx\-]{1,3}", compact):
        mask = 0
        for char in compact:
            if char == "r":
                mask |= PROT_READ
            elif char == "w":
                mask |= PROT_WRITE
            elif char == "x":
                mask |= PROT_EXEC
        return mask
    if normalized in NAME_TO_BIT:
        return NAME_TO_BIT[normalized]
    collapsed = re.sub(r"[\s|,+&/]+", "_", normalized)
    if collapsed in NAME_TO_BIT:
        return NAME_TO_BIT[collapsed]
    raise ProtectionParseError(f"unknown named protection bit: {token!r}")


def parse_protection(value: Any) -> Dict[str, Any]:
    """Parse a named set, octal string, or integer mask.

    Strict syntax conversion is exact, so parser-derived error bound is zero.
    Optional sender observation uncertainty is represented by callers.
    """
    raw = value

    if isinstance(value, bool):
        raise ProtectionParseError("boolean is not a protection mask")

    if isinstance(value, int):
        mask = value
        caliber = "integer_mask"
    elif isinstance(value, (list, tuple, set, frozenset)):
        mask = 0
        for item in value:
            if not isinstance(item, str):
                raise ProtectionParseError("named protection set must contain strings")
            mask |= _parse_named_token(item)
        caliber = "named_set"
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ProtectionParseError("empty protection representation")

        explicit_octal = re.fullmatch(r"(?i:0o)([0-7]+)", text)
        leading_zero_octal = re.fullmatch(r"0([0-7]+)", text)
        if explicit_octal:
            mask = int(explicit_octal.group(1), 8)
            caliber = "octal_string"
        elif leading_zero_octal and len(text) > 1:
            mask = int(text, 8)
            caliber = "octal_string"
        elif re.fullmatch(r"[+-]?\d+", text):
            mask = int(text, 10)
            caliber = "integer_mask"
        else:
            mask = 0
            tokens = [part for part in re.split(r"[\s,|+&/]+", text) if part]
            for token in tokens:
                mask |= _parse_named_token(token)
            caliber = "named_set"
    else:
        raise ProtectionParseError(
            "protection must be a named set, octal string, or integer mask"
        )

    if mask < 0:
        raise ProtectionParseError("protection mask must be non-negative")
    if mask & ~KNOWN_PROT_BITS:
        raise ProtectionParseError(
            f"protection mask contains unsupported canonical bits: {mask:#o}"
        )

    return {
        "mask": mask,
        "mask_octal": f"0o{mask:o}",
        "mask_named": mask_names(mask),
        "mask_compact": format_mask(mask),
        "caliber": caliber,
        "raw": raw,
        "parse_error_bound": 0,
    }


def caliber_diagnostics() -> List[Dict[str, Any]]:
    return [
        {
            "caliber": "named_set",
            "error_sources": [
                "unknown or vendor-specific protection name",
                "ambiguous Windows enum name to RWX semantic mapping",
            ],
            "strict_handling": "reject E_PROT_REPRESENTATION_INVALID",
            "upper_error_bound": 0,
            "bound_unit": "canonical_mask_integer_distance",
        },
        {
            "caliber": "octal_string",
            "error_sources": [
                "non-octal digit",
                "missing explicit base when a leading-zero decimal was intended",
                "fixed-width truncation",
            ],
            "strict_handling": "reject rather than infer a rounded value",
            "upper_error_bound": 0,
            "bound_unit": "canonical_mask_integer_distance",
        },
        {
            "caliber": "integer_mask",
            "error_sources": [
                "signed/unsigned width reinterpretation",
                "JSON float truncation",
                "unsupported high modifier bit",
            ],
            "strict_handling": "require a non-negative integer in canonical bit range",
            "upper_error_bound": 0,
            "bound_unit": "canonical_mask_integer_distance",
        },
    ]


def integer_distance_to_verdict_flip(
    mask: int, allowed_masks: Iterable[int]
) -> int:
    """Minimum absolute integer perturbation that changes allow/deny verdict."""
    allowed = set(allowed_masks)
    central_allowed = mask in allowed
    best: Optional[int] = None
    for candidate in range(0, KNOWN_PROT_BITS + 1):
        candidate_allowed = candidate in allowed
        if candidate_allowed != central_allowed:
            distance = abs(candidate - mask)
            if best is None or distance < best:
                best = distance
    return best if best is not None else 10**9


def boundary_analysis(
    stage_records: List[Dict[str, Any]],
    evaluated_masks: Dict[str, int],
    allowed_masks: Iterable[int],
) -> Dict[str, Any]:
    """Compose bounds and test whether lower/upper perturbation paths differ."""

    cumulative = 0
    stages = []
    for stage in stage_records:
        bound = int(stage.get("error_bound", stage.get("parse_error_bound", 0)) or 0)
        cumulative += bound
        stages.append(
            {
                "stage": stage.get("stage"),
                "caliber": stage.get("caliber"),
                "mask": stage.get("mask"),
                "error_bound": bound,
            }
        )

    after_mask = int(evaluated_masks["after"])
    before_mask = int(evaluated_masks["before"])
    allowed = list(allowed_masks)
    margin_after = integer_distance_to_verdict_flip(after_mask, allowed)
    margin_before = integer_distance_to_verdict_flip(before_mask, allowed)
    margin = min(margin_after, margin_before)

    def path_verdict(mask: int) -> str:
        return "allowed" if mask in set(allowed) else "denied"

    lower_after = max(0, after_mask - cumulative)
    upper_after = min(KNOWN_PROT_BITS, after_mask + cumulative)
    lower_before = max(0, before_mask - cumulative)
    upper_before = min(KNOWN_PROT_BITS, before_mask + cumulative)
    paths = {
        "central": {
            "before": before_mask,
            "after": after_mask,
            "after_verdict": path_verdict(after_mask),
            "before_verdict": path_verdict(before_mask),
        },
        "lower_endpoint": {
            "before": lower_before,
            "after": lower_after,
            "after_verdict": path_verdict(lower_after),
            "before_verdict": path_verdict(lower_before),
        },
        "upper_endpoint": {
            "before": upper_before,
            "after": upper_after,
            "after_verdict": path_verdict(upper_after),
            "before_verdict": path_verdict(upper_before),
        },
    }
    endpoint_verdicts = {
        paths["lower_endpoint"]["after_verdict"],
        paths["upper_endpoint"]["after_verdict"],
    }
    crossed = cumulative > 0 and (
        len(endpoint_verdicts) > 1 or cumulative >= margin
    )
    return {
        "caliber_diagnostics": caliber_diagnostics(),
        "stages": stages,
        "cumulative_error_bound": cumulative,
        "boundary_margin": margin,
        "boundary_crossed": bool(crossed),
        "paths": paths,
        "two_path_conclusions_differ": bool(len(endpoint_verdicts) > 1),
        "permission_bits_mask": PERMISSION_BITS,
    }
