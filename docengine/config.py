"""Configuration injection: every path and the clock tolerance come from a
JSON config file; nothing is read from the environment or the wall clock."""

import json
import os

from .errors import BusinessError, ErrorCode

_REQUIRED_KEYS = [
    "v1_snapshot",
    "v2_shard_dir",
    "v2_meta_db",
    "fragment_dir",
    "manifest",
    "templates_dir",
    "state_db",
    "lock_file",
    "clock_tolerance_ms",
]


class Config:
    def __init__(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        missing = [k for k in _REQUIRED_KEYS if k not in raw]
        if missing:
            raise BusinessError(
                ErrorCode.E_QUERY_SYNTAX,
                "config missing keys",
                missing=missing,
            )
        self.path = path
        base = os.path.dirname(os.path.abspath(path))
        self.base_dir = raw.get("base_dir", base)
        if not os.path.isabs(self.base_dir):
            self.base_dir = os.path.normpath(os.path.join(base, self.base_dir))
        for key in _REQUIRED_KEYS:
            value = raw[key]
            if key == "clock_tolerance_ms":
                setattr(self, key, int(value))
            else:
                setattr(self, key, self._resolve(value))

    def _resolve(self, p):
        if os.path.isabs(p):
            return p
        return os.path.normpath(os.path.join(self.base_dir, p))

    def rel(self, path):
        """Path relative to the config base dir, for stable diagnostics."""
        try:
            return os.path.relpath(path, self.base_dir)
        except ValueError:
            return path
