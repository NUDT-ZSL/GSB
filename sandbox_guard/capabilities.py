"""Capability whitelist and stable integer bit mappings."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .codes import E_CAP_MASK
from .errors import GuardError


CAPABILITY_WHITELIST_VERSION = "cap-whitelist-v1"
MERGE_POLICY_VERSION = "merge-policy-v1"
CHECKPOINT_FORMAT_VERSION = 1
METHOD_SYNC_MARKER = "SBXGUARD-METHOD-SYNC-V1"


CAPABILITY_BITS: dict[str, int] = {
    "file_read": 1 << 0,
    "file_write": 1 << 1,
    "network_outbound": 1 << 2,
    "process_spawn": 1 << 3,
    "symlink_follow": 1 << 4,
    "registry_read": 1 << 5,
    "registry_write": 1 << 6,
}

BIT_TO_CAPABILITY = {bit: name for name, bit in CAPABILITY_BITS.items()}
ALL_CAPABILITY_MASK = sum(CAPABILITY_BITS.values())


def mask_from_names(names: Iterable[object]) -> int:
    if not isinstance(names, (list, tuple, set, frozenset)):
        raise GuardError(
            E_CAP_MASK,
            "capabilities must be an array of named capabilities",
            details={"received_type": type(names).__name__},
        )
    mask = 0
    for name in names:
        if not isinstance(name, str) or name not in CAPABILITY_BITS:
            raise GuardError(
                E_CAP_MASK,
                "unknown or non-string capability name",
                details={"capability": repr(name), "known": sorted(CAPABILITY_BITS)},
            )
        mask |= CAPABILITY_BITS[name]
    return mask


def names_from_mask(mask: int) -> list[str]:
    return [name for name, bit in CAPABILITY_BITS.items() if mask & bit]


def normalize_capability_fields(
    names: object | None = None,
    mask: object | None = None,
    *,
    names_field: str = "capabilities",
    mask_field: str = "capability_mask",
) -> tuple[list[str], int]:
    """Validate named capabilities against their integer mask.

    When only one representation is supplied, the missing representation is
    derived.  When both are supplied, they must describe exactly the same set.
    """

    if names is not None and not isinstance(names, list):
        raise GuardError(
            E_CAP_MASK,
            f"{names_field} must be a list of strings",
            details={"field": names_field},
        )
    if mask is not None and (
        not isinstance(mask, int) or isinstance(mask, bool) or mask < 0
    ):
        raise GuardError(
            E_CAP_MASK,
            f"{mask_field} must be a nonnegative integer",
            details={"field": mask_field},
        )
    if mask is not None and mask & ~ALL_CAPABILITY_MASK:
        unknown_bits = mask & ~ALL_CAPABILITY_MASK
        raise GuardError(
            E_CAP_MASK,
            f"{mask_field} contains bits outside the whitelist",
            details={"field": mask_field, "unknown_mask": unknown_bits},
        )

    if names is None and mask is None:
        return [], 0

    name_mask = mask_from_names(names or [])
    if names is not None and mask is not None and name_mask != mask:
        raise GuardError(
            E_CAP_MASK,
            "named capabilities and integer mask do not match",
            details={
                "field": f"{names_field}/{mask_field}",
                "named_mask": name_mask,
                "declared_mask": mask,
            },
        )

    final_mask = mask if mask is not None else name_mask
    assert final_mask is not None
    return names_from_mask(final_mask), final_mask


def stable_capability_names(names: Sequence[str]) -> list[str]:
    """Return names ordered by their stable bit position."""

    mask = mask_from_names(names)
    return names_from_mask(mask)
