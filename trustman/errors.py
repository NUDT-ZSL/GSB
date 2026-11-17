"""Stable business and runtime error codes used by the application."""

from __future__ import annotations


E_SCHEMA = "E_SCHEMA"
E_LOCK = "E_LOCK"

E_CHAIN_EMPTY = "E_CHAIN_EMPTY"
E_MISSING_CERT = "E_MISSING_CERT"
E_MISSING_INTERMEDIATE = "E_MISSING_INTERMEDIATE"
E_CHAIN_BROKEN = "E_CHAIN_BROKEN"
E_ROOT_UNTRUSTED = "E_ROOT_UNTRUSTED"
E_NO_SNAPSHOT = "E_NO_SNAPSHOT"

E_EXPIRED_LEAF = "E_EXPIRED_LEAF"
E_EXPIRED_INTERMEDIATE = "E_EXPIRED_INTERMEDIATE"
E_EXPIRED_ROOT = "E_EXPIRED_ROOT"
E_NOT_YET_VALID_LEAF = "E_NOT_YET_VALID_LEAF"
E_NOT_YET_VALID_INTERMEDIATE = "E_NOT_YET_VALID_INTERMEDIATE"
E_NOT_YET_VALID_ROOT = "E_NOT_YET_VALID_ROOT"

E_REVOKED_LEAF = "E_REVOKED_LEAF"
E_REVOKED_INTERMEDIATE = "E_REVOKED_INTERMEDIATE"
E_REVOKED_ROOT = "E_REVOKED_ROOT"

E_TIMEOUT = "E_TIMEOUT"
E_BUDGET = "E_BUDGET"
E_QUEUE_FULL = "E_QUEUE_FULL"
E_INTERNAL = "E_INTERNAL"
E_ARGS = "E_ARGS"
E_NOT_FOUND = "E_NOT_FOUND"
E_REPORT_CORRUPT = "E_REPORT_CORRUPT"

E_CHECKPOINT_MISSING = "E_CHECKPOINT_MISSING"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_CHECKPOINT_STRUCTURE_CHANGED = "E_CHECKPOINT_STRUCTURE_CHANGED"
E_CHECKPOINT_POLICY_CHANGED = "E_CHECKPOINT_POLICY_CHANGED"
E_CHECKPOINT_INPUT_CHANGED = "E_CHECKPOINT_INPUT_CHANGED"
E_CHECKPOINT_REPLAY_MISMATCH = "E_CHECKPOINT_REPLAY_MISMATCH"


class TrustmanError(Exception):
    """Base class for errors carrying a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LockConflict(TrustmanError):
    def __init__(self, message: str = "workspace lock is already held"):
        super().__init__(E_LOCK, message)


class TaskTimeout(TrustmanError):
    def __init__(self, message: str = "wall-clock task limit exceeded"):
        super().__init__(E_TIMEOUT, message)
