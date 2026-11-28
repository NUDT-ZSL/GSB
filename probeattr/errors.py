"""Structured batch errors emitted to stderr as JSON."""

import json
import sys


class BatchError(Exception):
    """A validation failure that rejects the whole batch."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        payload = {"error_code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def emit_error(err):
    line = json.dumps(err.to_dict(), ensure_ascii=False, sort_keys=True)
    print(line, file=sys.stderr)
