"""Stable business errors exposed by the command line interface."""

from __future__ import annotations

from typing import Any, Mapping


DIAGNOSTIC_FIELDS = (
    "template_name",
    "version",
    "shard_index",
    "request_seq",
)


class BusinessError(Exception):
    """An expected input, policy, scheduling, or integrity error.

    Every error carries the four diagnostics fields required by the business
    contract. Callers may add operation-specific keys without changing the
    stable envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        exit_code: int = 2,
        template_name: str | None = None,
        version: str | int | None = None,
        shard_index: int | None = None,
        request_seq: int | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        diagnostics: dict[str, Any] = {
            "template_name": template_name,
            "version": version,
            "shard_index": shard_index,
            "request_seq": request_seq,
        }
        diagnostics.update(details)
        self.diagnostics = diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "diagnostics": self.diagnostics,
            },
        }


def require(condition: bool, code: str, message: str, **kwargs: Any) -> None:
    """Raise a BusinessError when *condition* is false."""

    if not condition:
        raise BusinessError(code, message, **kwargs)


def diagnostics_from(
    values: Mapping[str, Any],
    *,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a full diagnostic dictionary with stable required keys."""

    result = {field: values.get(field) for field in DIAGNOSTIC_FIELDS}
    if extras:
        result.update(extras)
    return result
