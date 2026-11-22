from __future__ import annotations

from typing import Any


class BusinessError(Exception):
    """A stable business error surfaced by the CLI and HTTP API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def bad_config(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_BAD_CONFIG", message, details=details)


def bad_clock(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_BAD_CLOCK", message, details=details)


def digest_mismatch(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_DIGEST_MISMATCH", message, details=details)


def not_initialized(message: str = "配置中心尚未初始化") -> BusinessError:
    return BusinessError("E_NOT_INITIALIZED", message, http_status=409)


def version_rollback(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_VERSION_ROLLBACK", message, details=details, http_status=409)


def version_conflict(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_VERSION_CONFLICT", message, details=details, http_status=409)


def already_initialized(message: str, **details: Any) -> BusinessError:
    return BusinessError(
        "E_ALREADY_INITIALIZED", message, details=details, http_status=409
    )


def round_conflict(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_ROUND_CONFLICT", message, details=details, http_status=409)


def not_found(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_NOT_FOUND", message, details=details, http_status=404)

