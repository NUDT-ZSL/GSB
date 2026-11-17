"""Stable identifiers, error codes, and limits used by the workbench."""

from __future__ import annotations

import re

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
POLICY_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{2,95}$")
EVENT_ID_RE = re.compile(r"^evt-[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
UPDATE_ID_RE = re.compile(r"^upd-[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
MARKER_ID_RE = re.compile(r"^sync-[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
SESSION_ID_RE = re.compile(r"^sess-[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
TOKEN_ID_RE = re.compile(r"^tok-[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")

ENTRY_KINDS = {
    "session_policy",
    "role",
    "token_rule",
    "role_binding",
}

EVENT_TYPES = {
    "login",
    "renew",
    "role_change",
    "logout",
    "token_revoke",
}

CHECKPOINT_STRUCTURE_VERSION = "1.0"
REPORT_STRUCTURE_VERSION = "1.0"
GRAPH_STRUCTURE_VERSION = "1.0"

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_ARGUMENT_ERROR = 2

FATAL_CODES = {
    "E_JSON",
    "E_IO",
    "E_SCHEMA",
    "E_ID_FORMAT",
    "E_UNKNOWN_KIND",
    "E_VERSION",
    "E_DUP",
    "E_ROLE_REF",
    "E_TOKEN_REF",
    "E_BINDING_REF",
    "E_TIMESTAMP",
    "E_UPDATE_ID_FORMAT",
    "E_UPDATE_DUP",
    "E_UPDATE_OP",
    "E_MARKER",
    "E_MARKER_VERSION",
    "E_SYNC",
    "E_EVENT_ID_FORMAT",
    "E_SESSION_FORMAT",
    "E_TOKEN_FORMAT",
    "E_EVENT_TYPE",
    "E_EVENT_REF",
    "E_EVENT_STATE",
    "E_EVENT_DUP",
    "E_CYCLE",
    "E_CHECKPOINT_WRITE",
    "E_INTERNAL",
}

NON_FATAL_CODES = {
    "E_LOCK",
    "E_CHECKPOINT_STRUCTURE",
    "E_CHECKPOINT_VERSION",
    "E_CHECKPOINT_IDENTITY",
    "E_CHECKPOINT_UPDATE",
    "E_CHECKPOINT_CORRUPT",
}
