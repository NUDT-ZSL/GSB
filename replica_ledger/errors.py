"""Business exceptions used by the replica ledger component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_SPEC = "E_SPEC"
E_BLOCK = "E_BLOCK"
E_INTEGRITY = "E_INTEGRITY"
E_GRID = "E_GRID"
E_PROTOCOL = "E_PROTOCOL"
E_LEDGER = "E_LEDGER"


@dataclass
class LedgerDomainError(Exception):
    """A failure that deliberately maps to a stable business code.

    ``field_path`` always points at the offending JSON field or session field.
    ``node_id`` and ``block_id`` use ``*`` when the failure is group/session
    scoped so structured logs always retain the three required keys.
    """

    code: str
    message: str
    field_path: str = "$"
    node_id: str = "*"
    block_id: str = "*"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field_path": self.field_path,
            "node_id": self.node_id,
            "block_id": self.block_id,
            "details": self.details,
        }


class SpecError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str = "$",
        *,
        node_id: str = "*",
        block_id: str = "*",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(E_SPEC, message, field_path, node_id, block_id, details or {})


class BlockError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str,
        *,
        node_id: str = "*",
        block_id: str = "*",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(E_BLOCK, message, field_path, node_id, block_id, details or {})


class IntegrityError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str,
        *,
        node_id: str = "*",
        block_id: str = "*",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            E_INTEGRITY, message, field_path, node_id, block_id, details or {}
        )


class GridError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str = "session.scale",
        *,
        node_id: str = "*",
        block_id: str = "*",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(E_GRID, message, field_path, node_id, block_id, details or {})


class ProtocolError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str = "protocol",
        *,
        node_id: str = "*",
        block_id: str = "*",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            E_PROTOCOL, message, field_path, node_id, block_id, details or {}
        )


class StoredLedgerError(LedgerDomainError):
    def __init__(
        self,
        message: str,
        field_path: str = "ledger",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(E_LEDGER, message, field_path, "*", "*", details or {})
