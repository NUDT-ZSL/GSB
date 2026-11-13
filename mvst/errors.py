"""Business error type and deterministic stderr emission.

Every user-facing failure is reported as a single-line JSON object on stderr
containing at least ``error_code`` (always starting with ``E_``) and any extra
context fields.  No uncaught traceback is ever printed by the CLI layer.
"""

from __future__ import annotations

import json
import sys
from typing import Any


class MvstError(Exception):
    """A business/input error carrying a structured payload."""

    def __init__(self, error_code: str, report: Any = None, **fields: Any) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.fields = fields
        # Optional full validation report: the CLI emits it on stdout before
        # writing the single-line error on stderr (e.g. partial worker stats).
        self.report = report

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code}
        # Stable key order for deterministic stderr output.
        for key in sorted(self.fields):
            payload[key] = self.fields[key]
        return payload


def emit_error(error_code: str, **fields: Any) -> None:
    """Write exactly one JSON line describing the error to stderr."""
    payload: dict[str, Any] = {"error_code": error_code}
    for key in sorted(fields):
        payload[key] = fields[key]
    sys.stderr.write(json.dumps(payload, ensure_ascii=False, sort_keys=False) + "\n")
    sys.stderr.flush()


def emit_exception(exc: MvstError) -> None:
    sys.stderr.write(json.dumps(exc.to_payload(), ensure_ascii=False) + "\n")
    sys.stderr.flush()
