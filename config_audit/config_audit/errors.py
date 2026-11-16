"""Business error codes and structured exceptions for the audit component.

All validation / runtime failures carry a stable business code so that callers
can branch on semantics rather than parsing free text.  Every exception keeps a
list of structured ``locations`` (JSON-pointer-like strings) so that an invalid
archive can be rejected *as a whole* while still reporting every error position.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Stable business codes (part of the public contract) -------------------
E_CONFIG = "E_CONFIG"        # archive schema / strong validation failure
E_RANGE = "E_RANGE"          # numeric value outside its supported bounds
E_CYCLE = "E_CYCLE"          # derived dependency graph contains a cycle
E_GRID = "E_GRID"            # precision grid parameter missing / malformed
E_PROPAGATE = "E_PROPAGATE"  # error propagation reached an undeterminable node
E_REPLAY = "E_REPLAY"        # snapshot replay produced a different conclusion


@dataclass
class AuditError(Exception):
    """Base class for every structured business error."""

    code: str
    message: str
    locations: list[str] = field(default_factory=list)
    key: str | None = None
    grid: int | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        loc = ""
        if self.locations:
            loc = " | at " + "; ".join(self.locations)
        extra = ""
        if self.key is not None:
            extra += f" [key={self.key}]"
        if self.grid is not None:
            extra += f" [grid={self.grid}]"
        return f"{self.code}: {self.message}{extra}{loc}"

    def to_dict(self) -> dict:
        out: dict = {
            "code": self.code,
            "message": self.message,
            "locations": list(self.locations),
        }
        if self.key is not None:
            out["key"] = self.key
        if self.grid is not None:
            out["grid"] = self.grid
        return out


class ConfigError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_CONFIG, message, locations or [], key, grid)


class RangeError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_RANGE, message, locations or [], key, grid)


class CycleError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_CYCLE, message, locations or [], key, grid)


class GridError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_GRID, message, locations or [], key, grid)


class PropagateError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_PROPAGATE, message, locations or [], key, grid)


class ReplayError(AuditError):
    def __init__(self, message: str, locations: list[str] | None = None,
                 key: str | None = None, grid: int | None = None) -> None:
        super().__init__(E_REPLAY, message, locations or [], key, grid)
