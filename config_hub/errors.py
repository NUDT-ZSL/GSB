"""Business errors shared by the CLI and the HTTP API."""

from __future__ import annotations

from typing import Any


class ConfigHubError(Exception):
    """An expected, machine-readable business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        exit_code: int = 2,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.exit_code = exit_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


def bad_config(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError("E_BAD_CONFIG", message, details=details)


def bad_patch(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError("E_BAD_PATCH", message, details=details)


def bad_input(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError("E_BAD_INPUT", message, details=details)


def bad_clock(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError("E_BAD_CLOCK", message, details=details)


def version_conflict(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_VERSION_CONFLICT", message, http_status=409, exit_code=3, details=details
    )


def clock_rollback(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_CLOCK_ROLLBACK", message, http_status=409, exit_code=4, details=details
    )


def patch_not_effective(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_PATCH_NOT_EFFECTIVE",
        message,
        http_status=409,
        exit_code=5,
        details=details,
    )


def result_conflict(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_RESULT_CONFLICT", message, http_status=409, exit_code=6, details=details
    )


def not_found(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_NOT_FOUND", message, http_status=404, exit_code=7, details=details
    )


def verify_failed(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_VERIFY_FAILED", message, http_status=409, exit_code=8, details=details
    )


def incomplete_work(message: str, details: Any | None = None) -> ConfigHubError:
    return ConfigHubError(
        "E_INCOMPLETE_WORK", message, http_status=409, exit_code=9, details=details
    )
