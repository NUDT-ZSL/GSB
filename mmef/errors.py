"""Stage-scoped exception carrying a business code."""
from __future__ import annotations


class StageError(Exception):
    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code
