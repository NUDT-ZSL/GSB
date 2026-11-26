from __future__ import annotations


class DiagnosticError(ValueError):
    """Base class for deterministic diagnostic engine errors."""


class SampleValidationError(DiagnosticError):
    """Raised when declarative samples violate the input contract."""


class PolicyIntegrityError(DiagnosticError):
    """Raised when a policy shard, hash, generation, or digest is invalid."""


class PolicyVersionRollbackError(PolicyIntegrityError):
    """Raised when a policy package attempts to move the pointer backwards."""


class RecoveryConsistencyError(DiagnosticError):
    """Raised when persisted state cannot be reconciled during recovery."""
