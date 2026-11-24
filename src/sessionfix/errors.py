from __future__ import annotations


class SessionFixError(Exception):
    def __init__(self, code: str, message: str, source: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source = source

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {"code": self.code, "message": self.message}
        if self.source is not None:
            value["source"] = self.source
        return value


class LockError(SessionFixError):
    def __init__(self, path: str) -> None:
        super().__init__("E_LOCK", f"work directory is already locked: {path}", path)
