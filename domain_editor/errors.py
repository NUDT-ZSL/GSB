"""Business exceptions and stable business codes."""

from __future__ import annotations


E_FORMAT_TRUNCATED = "E_FORMAT_TRUNCATED"
E_FORMAT_OVERLONG = "E_FORMAT_OVERLONG"
E_JSON_INVALID = "E_JSON_INVALID"
E_SPAN_ORDER = "E_SPAN_ORDER"
E_CONF_RANGE = "E_CONF_RANGE"
E_EDIT_INVALID = "E_EDIT_INVALID"
E_ARG_DANGLING = "E_ARG_DANGLING"
E_ROLE_UNKNOWN = "E_ROLE_UNKNOWN"
E_DOCUMENT_UNKNOWN = "E_DOCUMENT_UNKNOWN"
E_DUPLICATE_ID = "E_DUPLICATE_ID"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"
E_INTERNAL = "E_INTERNAL"

ALL_CODES = (
    E_FORMAT_TRUNCATED,
    E_FORMAT_OVERLONG,
    E_JSON_INVALID,
    E_SPAN_ORDER,
    E_CONF_RANGE,
    E_EDIT_INVALID,
    E_ARG_DANGLING,
    E_ROLE_UNKNOWN,
    E_DOCUMENT_UNKNOWN,
    E_DUPLICATE_ID,
    E_SNAPSHOT_STALE,
    E_SNAPSHOT_INVALID,
    E_INTERNAL,
)


class DomainError(Exception):
    """An expected, item-local data error."""

    code = E_INTERNAL

    def __init__(self, message: str, *, code: str | None = None, detail_code: str | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.detail_code = detail_code


class FormatTruncatedError(DomainError):
    code = E_FORMAT_TRUNCATED


class FormatOverlongError(DomainError):
    code = E_FORMAT_OVERLONG


class JsonInvalidError(DomainError):
    code = E_JSON_INVALID


class SpanOrderError(DomainError):
    code = E_SPAN_ORDER


class ConfidenceRangeError(DomainError):
    code = E_CONF_RANGE


class EditInvalidError(DomainError):
    code = E_EDIT_INVALID


class ArgumentDanglingError(DomainError):
    code = E_ARG_DANGLING


class RoleUnknownError(DomainError):
    code = E_ROLE_UNKNOWN


class DocumentUnknownError(DomainError):
    code = E_DOCUMENT_UNKNOWN


class DuplicateIdError(DomainError):
    code = E_DUPLICATE_ID


class SnapshotStaleError(DomainError):
    code = E_SNAPSHOT_STALE


class SnapshotInvalidError(DomainError):
    code = E_SNAPSHOT_INVALID
