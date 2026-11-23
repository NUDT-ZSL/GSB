"""Business error codes and exception hierarchy for the scaling decision component.

Every validation or rule violation raises :class:`ScalerError` carrying a stable
string ``code`` so callers (CLI, tests) can branch on machine readable codes
instead of parsing human readable messages.
"""


class ScalerError(Exception):
    """Base class for all business errors.

    :param code: stable business error code, e.g. ``E_BAD_SAMPLE``.
    :param message: human readable description.
    :param details: optional structured context (never persisted on failure).
    """

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def bad_sample(message, **details):
    return ScalerError("E_BAD_SAMPLE", message, details)


def version_rollback(message, **details):
    return ScalerError("E_VERSION_ROLLBACK", message, details)


def bad_shard(message, **details):
    return ScalerError("E_BAD_SHARD", message, details)


def budget_exhausted(message, **details):
    return ScalerError("E_BUDGET_EXHAUSTED", message, details)


def placement_unsatisfiable(message, **details):
    return ScalerError("E_PLACEMENT_UNSATISFIABLE", message, details)


def state_conflict(message, **details):
    return ScalerError("E_STATE_CONFLICT", message, details)


def corruption(message, **details):
    return ScalerError("E_CORRUPT_STATE", message, details)
