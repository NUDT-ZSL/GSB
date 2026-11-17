"""Business errors used by the shard router.

All public entry points raise :class:`RouterError` for expected failures.  The
``code`` values are part of the teaching/demo contract and must remain stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


E_CONFIG = "E_CONFIG"
E_SQL = "E_SQL"
E_RING = "E_RING"
E_CACHE = "E_CACHE"
E_DEPEND = "E_DEPEND"
E_VERSION = "E_VERSION"
E_SNAPSHOT = "E_SNAPSHOT"


@dataclass(frozen=True)
class RouterError(Exception):
    """An expected, structured business error.

    ``field_path`` points at the offending JSON field for configuration and
    snapshot errors.  SQL errors additionally populate ``position`` with a
    one-based line/column pair and the original character offset.
    """

    code: str
    message: str
    field_path: str | None = None
    position: Mapping[str, int] | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "status": "error",
        }
        if self.field_path is not None:
            result["field_path"] = self.field_path
        if self.position is not None:
            result["position"] = dict(self.position)
        if self.details:
            result["details"] = _freeze_json(self.details)
        return result


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _freeze_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_freeze_json(item) for item in value]
    return value


def sql_error(message: str, sql: str, offset: int) -> RouterError:
    offset = max(0, min(int(offset), len(sql)))
    line = sql.count("\n", 0, offset) + 1
    last_newline = sql.rfind("\n", 0, offset)
    column = offset - last_newline
    return RouterError(
        code=E_SQL,
        message=message,
        position={"offset": offset, "line": line, "column": column},
    )
