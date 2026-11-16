"""High-level execution helpers shared by CLI and Tkinter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import SchedulerConfig
from .scheduler import EventCallback, Scheduler
from .script import load_script, validate_script
from .storage import SQLiteStore
from .util import read_text, resolve_relative_path


def resolve_initial_sql(
    script_path: str | Path,
    raw_script: dict[str, Any],
    explicit_init_sql: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(sql_text, source_path)`` from CLI or script metadata."""

    if explicit_init_sql is not None:
        path = Path(explicit_init_sql)
        return read_text(path, code="E_SCRIPT", label="initial SQL"), str(path)

    candidate = raw_script.get("initial_sql")
    if candidate is None:
        return None, None
    if not isinstance(candidate, str):
        return None, None
    path = resolve_relative_path(Path(script_path), candidate)
    return read_text(path, code="E_SCRIPT", label="initial SQL"), str(path)


def execute_script_file(
    script_path: str | Path,
    *,
    db_path: str | Path,
    init_sql_path: str | Path | None = None,
    config: SchedulerConfig | None = None,
    on_event: EventCallback | None = None,
) -> dict[str, Any]:
    raw_script = load_script(script_path)
    initial_sql, initial_sql_source = resolve_initial_sql(
        script_path,
        raw_script,
        init_sql_path,
    )
    config = config or SchedulerConfig()

    with SQLiteStore(db_path) as store:
        if initial_sql is not None:
            store.reset_from_sql(initial_sql)
        script = validate_script(raw_script, store.row_keys())
        scheduler = Scheduler(script, store, config, on_event=on_event)
        result = scheduler.run()
        result["initial_sql_source"] = initial_sql_source
        result["database_path"] = str(db_path)
        return result


def validate_script_file(
    script_path: str | Path,
    *,
    db_path: str | Path | None = None,
    init_sql_path: str | Path | None = None,
) -> dict[str, Any]:
    raw_script = load_script(script_path)
    initial_sql, _ = resolve_initial_sql(script_path, raw_script, init_sql_path)
    with SQLiteStore(db_path) as store:
        if initial_sql is not None:
            store.reset_from_sql(initial_sql)
        script = validate_script(raw_script, store.row_keys())
    return {
        "code": "OK",
        "script_name": script.name,
        "session_count": len(script.sessions),
        "sessions": [
            {
                "session_id": session.session_id,
                "isolation": session.isolation,
                "operation_count": len(session.operations),
            }
            for session in script.sessions
        ],
    }
