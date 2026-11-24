"""Domain errors carrying stable machine-readable codes."""


class DungeonError(Exception):
    """Raised for all predictable pipeline failures.

    ``code`` is one of ``E_SCHEMA``, ``E_DIGEST`` and ``E_SEAM`` so that
    callers can serialize the failure to a JSON error document.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"ok": False, "code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class GracefulInterrupt(Exception):
    """Raised when an injectable stop event fires at a tile boundary."""
