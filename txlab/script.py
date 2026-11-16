"""Loading and strict validation of deterministic edit-script JSON files."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Collection

from .errors import ScriptError
from .models import (
    ISOLATION_LEVELS,
    OPERATION_NAMES,
    TERMINAL_OPERATIONS,
    EditScript,
    Operation,
    SessionSpec,
)
from .util import canonical_json, read_json


SCRIPT_FORMAT_VERSION = 1


def load_script(path: str | Path) -> dict[str, Any]:
    raw = read_json(path, code="E_SCRIPT", label="script")
    if not isinstance(raw, dict):
        raise ScriptError("script root must be a JSON object")
    return raw


def validate_script(
    raw: dict[str, Any],
    existing_keys: Collection[str] | None = None,
) -> EditScript:
    """Validate a whole script before any transaction starts.

    ``existing_keys`` is required to enforce the target-key existence rule.
    """

    if not isinstance(raw, dict):
        raise ScriptError("script root must be a JSON object")

    script_version = raw.get("script_version", SCRIPT_FORMAT_VERSION)
    if script_version != SCRIPT_FORMAT_VERSION:
        raise ScriptError(
            f"unsupported script_version {script_version!r}",
            details={"supported": SCRIPT_FORMAT_VERSION},
        )

    name = raw.get("name", "unnamed-script")
    if not isinstance(name, str) or not name.strip():
        raise ScriptError("script.name must be a non-empty string")

    initial_sql = raw.get("initial_sql")
    if initial_sql is not None and not isinstance(initial_sql, str):
        raise ScriptError("script.initial_sql must be a string when present")

    sessions_raw = raw.get("sessions")
    if not isinstance(sessions_raw, list) or not sessions_raw:
        raise ScriptError("script.sessions must be a non-empty array")

    if existing_keys is not None:
        existing_keys = {str(key) for key in existing_keys}

    seen_session_ids: set[str] = set()
    sessions: list[SessionSpec] = []

    for session_index, session_raw in enumerate(sessions_raw):
        if not isinstance(session_raw, dict):
            raise ScriptError(f"session #{session_index} must be an object")

        session_id = session_raw.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ScriptError(
                f"session #{session_index} id must be a non-empty string"
            )
        session_id = session_id.strip()
        if session_id in seen_session_ids:
            raise ScriptError(
                f"duplicate session id {session_id!r}",
                session_id=session_id,
            )
        seen_session_ids.add(session_id)

        isolation = session_raw.get("isolation")
        if isolation not in ISOLATION_LEVELS:
            raise ScriptError(
                f"invalid isolation level {isolation!r}",
                session_id=session_id,
                details={"allowed": list(ISOLATION_LEVELS)},
            )

        operations_raw = session_raw.get("operations")
        if not isinstance(operations_raw, list) or not operations_raw:
            raise ScriptError(
                "operations must be a non-empty array",
                session_id=session_id,
            )

        operations: list[Operation] = []
        terminal_seen = False
        for op_index, op_raw in enumerate(operations_raw):
            if terminal_seen:
                raise ScriptError(
                    "no operation is allowed after commit or rollback",
                    session_id=session_id,
                    op_index=op_index,
                )
            if not isinstance(op_raw, dict):
                raise ScriptError(
                    "operation must be an object",
                    session_id=session_id,
                    op_index=op_index,
                )
            op_name = op_raw.get("op")
            if op_name not in OPERATION_NAMES:
                raise ScriptError(
                    f"invalid operation {op_name!r}",
                    session_id=session_id,
                    op_index=op_index,
                    details={"allowed": list(OPERATION_NAMES)},
                )

            key: str | None = None
            value: Any = None
            has_value = False
            if op_name in ("read", "write"):
                key_raw = op_raw.get("key")
                if not isinstance(key_raw, str) or not key_raw:
                    raise ScriptError(
                        f"{op_name} requires a non-empty string key",
                        session_id=session_id,
                        op_index=op_index,
                    )
                key = key_raw
                if existing_keys is not None and key not in existing_keys:
                    raise ScriptError(
                        f"target row key {key!r} does not exist",
                        session_id=session_id,
                        op_index=op_index,
                        details={"key": key},
                    )
                if op_name == "write":
                    if "value" not in op_raw:
                        raise ScriptError(
                            "write requires a value field",
                            session_id=session_id,
                            op_index=op_index,
                        )
                    value = op_raw["value"]
                    try:
                        canonical_json(value)
                    except (TypeError, ValueError) as exc:
                        raise ScriptError(
                            f"write.value is not valid JSON: {exc}",
                            session_id=session_id,
                            op_index=op_index,
                        ) from exc
                    has_value = True

            operations.append(
                Operation(
                    index=op_index,
                    name=op_name,
                    key=key,
                    value=value,
                    has_value=has_value,
                )
            )
            if op_name in TERMINAL_OPERATIONS:
                terminal_seen = True

        if operations[-1].name not in TERMINAL_OPERATIONS:
            raise ScriptError(
                "session must end with commit or rollback",
                session_id=session_id,
                op_index=len(operations) - 1,
            )

        sessions.append(
            SessionSpec(
                session_id=session_id,
                isolation=isolation,
                operations=tuple(operations),
            )
        )

    return EditScript(
        name=name.strip(),
        sessions=tuple(sessions),
        raw=raw,
        initial_sql=initial_sql,
    )
