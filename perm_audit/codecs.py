from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .codes import E_MASK_SYNTAX, E_PERMISSION_NAME

CANONICAL_BITS = 12
CANONICAL_MASK = (1 << CANONICAL_BITS) - 1
OCTAL_DIGIT_WIDTH = (CANONICAL_BITS + 2) // 3

# Bit positions are part of the stable ledger contract.
PERMISSION_BITS: dict[str, int] = {
    "read": 1 << 0,
    "write": 1 << 1,
    "append": 1 << 2,
    "execute": 1 << 3,
    "delete": 1 << 4,
    "read_attributes": 1 << 5,
    "write_attributes": 1 << 6,
    "read_permissions": 1 << 7,
    "change_permissions": 1 << 8,
    "take_ownership": 1 << 9,
    "synchronize": 1 << 10,
    "manage_audit": 1 << 11,
}

OPERATION_BITS: dict[str, int] = {
    "read": PERMISSION_BITS["read"],
    "list": PERMISSION_BITS["read"],
    "list_directory": PERMISSION_BITS["read"],
    "write": PERMISSION_BITS["write"],
    "create": PERMISSION_BITS["write"],
    "write_data": PERMISSION_BITS["write"],
    "append": PERMISSION_BITS["append"],
    "add_subdir": PERMISSION_BITS["append"],
    "mkdir": PERMISSION_BITS["append"],
    "execute": PERMISSION_BITS["execute"],
    "traverse": PERMISSION_BITS["execute"],
    "delete": PERMISSION_BITS["delete"],
    "delete_file": PERMISSION_BITS["delete"],
    "read_attributes": PERMISSION_BITS["read_attributes"],
    "write_attributes": PERMISSION_BITS["write_attributes"],
    "read_acl": PERMISSION_BITS["read_permissions"],
    "read_permissions": PERMISSION_BITS["read_permissions"],
    "change_permissions": PERMISSION_BITS["change_permissions"],
    "write_acl": PERMISSION_BITS["change_permissions"],
    "take_ownership": PERMISSION_BITS["take_ownership"],
    "chown": PERMISSION_BITS["take_ownership"],
    "synchronize": PERMISSION_BITS["synchronize"],
    "sync": PERMISSION_BITS["synchronize"],
    "manage_audit": PERMISSION_BITS["manage_audit"],
    "audit": PERMISSION_BITS["manage_audit"],
}

_NAME_ALIASES: dict[str, int] = {}
for _name, _bit in PERMISSION_BITS.items():
    _NAME_ALIASES[_name] = _bit
    _NAME_ALIASES[_name.replace("_", "-")] = _bit
    _NAME_ALIASES[_name.replace("_", "")] = _bit
_NAME_ALIASES.update(
    {
        "full": CANONICAL_MASK,
        "full_control": CANONICAL_MASK,
        "all": CANONICAL_MASK,
        "r": PERMISSION_BITS["read"],
        "w": PERMISSION_BITS["write"],
        "x": PERMISSION_BITS["execute"],
        "d": PERMISSION_BITS["delete"],
        "rd": PERMISSION_BITS["read"],
        "wd": PERMISSION_BITS["write"],
        "ad": PERMISSION_BITS["append"],
        "rea": PERMISSION_BITS["read_attributes"],
        "wea": PERMISSION_BITS["write_attributes"],
        "rc": PERMISSION_BITS["read_permissions"],
        "wdac": PERMISSION_BITS["change_permissions"],
        "wo": PERMISSION_BITS["take_ownership"],
        "sy": PERMISSION_BITS["synchronize"],
    }
)

BIT_NAMES: dict[int, str] = {bit: name for name, bit in PERMISSION_BITS.items()}

ERROR_BOUND_CONTRACT = {
    "canonical_bits": CANONICAL_BITS,
    "canonical_mask": CANONICAL_MASK,
    "integer_mask_high_bit_retention": (
        "observed=raw&0xFFF; high=raw&~0xFFF; "
        "uncertain=0 when high=0 else CANONICAL_MASK"
    ),
    "octal_digit_truncation": (
        "d=observed_octal_digit_count; covered=min(12,3d); "
        "uncertain=0xFFF ^ ((1<<covered)-1) for d<=4"
    ),
    "named_set_missing_bit_zero_fill": (
        "observed=OR(named bits); uncertain=CANONICAL_MASK ^ observed"
    ),
    "cumulative": "tight_error_bound=OR(applicable uncertain_mask); additive_bound_sum=SUM(error_bound)",
}


class MaskParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PermissionValue:
    style: str
    observed_mask: int
    error_bound: int
    uncertain_mask: int
    formula: str
    raw_observation: str
    retained_high_mask: int = 0
    unknown_names: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "observed_mask": self.observed_mask,
            "error_bound": self.error_bound,
            "uncertain_mask": self.uncertain_mask,
            "formula": self.formula,
            "raw_observation": self.raw_observation,
            "retained_high_mask": self.retained_high_mask,
            "unknown_names": list(self.unknown_names),
        }


def bit_name(bit: int) -> str:
    return BIT_NAMES.get(bit, f"unknown_bit_{bit}")


def bits_to_list(mask: int) -> list[dict[str, Any]]:
    return [
        {"bit": index, "weight": 1 << index, "name": bit_name(1 << index)}
        for index in range(CANONICAL_BITS)
        if mask & (1 << index)
    ]


def operation_bit(operation: Any) -> int:
    if not isinstance(operation, str):
        raise MaskParseError(E_MASK_SYNTAX, "operation must be a string")
    key = operation.strip().lower().replace("-", "_")
    if key not in OPERATION_BITS:
        raise MaskParseError(E_PERMISSION_NAME, f"unknown operation: {operation!r}")
    return OPERATION_BITS[key]


def parse_integer_mask(raw: Any) -> PermissionValue:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise MaskParseError(E_MASK_SYNTAX, "integer mask must be a JSON integer")
    if raw < 0:
        raise MaskParseError(E_MASK_SYNTAX, "integer mask must be non-negative")
    observed = raw & CANONICAL_MASK
    high = raw & ~CANONICAL_MASK
    if high:
        # High bits are retained in the observation, but their alias into the
        # canonical catalog is not provable from the integer form alone.
        uncertain = CANONICAL_MASK
        formula = "integer: high_bits_retained; uncertain=CANONICAL_MASK when raw>>12 != 0"
    else:
        uncertain = 0
        formula = "integer: high_bits_retained; raw<=CANONICAL_MASK => bound=0"
    return PermissionValue(
        style="integer",
        observed_mask=observed,
        error_bound=uncertain,
        uncertain_mask=uncertain,
        formula=formula,
        raw_observation=str(raw),
        retained_high_mask=high,
    )


def parse_octal_mask(raw: Any) -> PermissionValue:
    if not isinstance(raw, str):
        raise MaskParseError(E_MASK_SYNTAX, "octal mask must be a string")
    text = raw.strip()
    body = text
    if body[:2].lower() == "0o":
        body = body[2:]
    if not body or any(ch not in "01234567" for ch in body):
        raise MaskParseError(E_MASK_SYNTAX, f"invalid octal permission string: {raw!r}")
    digit_count = len(body)
    raw_int = int(body, 8)
    observed = raw_int & CANONICAL_MASK
    high = raw_int & ~CANONICAL_MASK

    if digit_count <= OCTAL_DIGIT_WIDTH:
        covered_bits = min(CANONICAL_BITS, 3 * digit_count)
        uncertain = CANONICAL_MASK & ~((1 << covered_bits) - 1)
        formula = (
            f"octal: digit_count={digit_count}; truncated leading digits => "
            f"uncertain=CANONICAL_MASK ^ ((1<<{covered_bits})-1)"
        )
    else:
        # More than four octal digits: low canonical digits are exact, while
        # observed high digits have no stable canonical position.
        uncertain = CANONICAL_MASK
        formula = (
            f"octal: digit_count={digit_count}>{OCTAL_DIGIT_WIDTH}; "
            "overflow aliases make canonical mapping uncertain"
        )
    return PermissionValue(
        style="octal",
        observed_mask=observed,
        error_bound=uncertain,
        uncertain_mask=uncertain,
        formula=formula,
        raw_observation=raw,
        retained_high_mask=high,
    )


def parse_named_mask(raw: Any) -> PermissionValue:
    if isinstance(raw, str):
        # Convenience for compact "read|write" writings.
        items = [part.strip() for part in raw.replace(",", "|").split("|") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, set):
        items = list(raw)
    else:
        raise MaskParseError(E_MASK_SYNTAX, "named permission set must be a list or string")

    observed = 0
    unknown: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise MaskParseError(E_PERMISSION_NAME, "permission names must be strings")
        key = item.strip().lower()
        bit = _NAME_ALIASES.get(key)
        if bit is None:
            unknown.append(item)
            continue
        observed |= bit
    if unknown:
        raise MaskParseError(
            E_PERMISSION_NAME, f"unknown permission name(s): {', '.join(unknown)}"
        )
    # Missing named positions are zero-filled in observed_mask. Under the
    # conservative provenance contract, a zero-filled position is also the
    # maximum possible truncation error at that position.
    uncertain = CANONICAL_MASK & ~observed
    return PermissionValue(
        style="named",
        observed_mask=observed,
        error_bound=uncertain,
        uncertain_mask=uncertain,
        formula="named: missing_bits_zero_filled; uncertain=CANONICAL_MASK ^ observed",
        raw_observation="[" + ",".join(str(x) for x in items) + "]",
    )


def parse_permission(style: Any, raw: Any) -> PermissionValue:
    if not isinstance(style, str):
        raise MaskParseError(E_MASK_SYNTAX, "mask_style must be a string")
    key = style.strip().lower()
    if key in {"int", "integer", "mask", "decimal"}:
        return parse_integer_mask(raw)
    if key in {"oct", "octal", "octal_string"}:
        return parse_octal_mask(raw)
    if key in {"named", "names", "set", "named_set"}:
        return parse_named_mask(raw)
    raise MaskParseError(E_MASK_SYNTAX, f"unknown mask_style: {style!r}")


def merge_uncertain_mask(values: Iterable[PermissionValue | int]) -> int:
    result = 0
    for value in values:
        result |= value.uncertain_mask if isinstance(value, PermissionValue) else int(value)
    return result
