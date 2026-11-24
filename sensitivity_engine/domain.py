"""Controlled vocabularies and canonical-key validation."""

from __future__ import annotations

import re


LEVEL_ENUM_VERSION = "level-enum-v1"
LAYOUT_VERSION = "layout-group-v1"
SNAPSHOT_FORMAT_VERSION = 1

# Priority increases with sensitivity.  There is deliberately no implicit
# "unmarked" level: an empty basis is reported with E_NO_BASIS and level null.
LEVEL_PRIORITY: dict[str, int] = {
    "PUBLIC": 10,
    "INTERNAL": 20,
    "CONFIDENTIAL": 30,
    "RESTRICTED": 40,
}
LEVELS = tuple(LEVEL_PRIORITY)

INHERITANCE_MODES = ("ALLOW", "BLOCK")
TRUSTEE_KINDS = ("USER", "GROUP")

OBJECT_KEY_RE = re.compile(r"^obj:[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)*$")
SOURCE_KEY_RE = re.compile(r"^src:[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)*$")
TRUSTEE_KEY_RE = re.compile(r"^(?:trustee|group):[a-z0-9][a-z0-9_.-]*$")
BATCH_ID_RE = re.compile(r"^batch:[A-Za-z0-9][A-Za-z0-9_.:-]*$")

OP_ADD_SOURCE = "add_source"
OP_REVOKE_SOURCE = "revoke_source"
OP_MIGRATE_TRUSTEE = "migrate_trustee"
OP_TYPES = (OP_ADD_SOURCE, OP_REVOKE_SOURCE, OP_MIGRATE_TRUSTEE)


def higher_or_equal_level(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return LEVEL_PRIORITY[left] >= LEVEL_PRIORITY[right]
