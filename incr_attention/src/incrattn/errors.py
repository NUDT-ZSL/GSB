"""Business error codes and exception hierarchy for incremental attention."""


E_OK = "E_OK"
E_RECOMPUTE_PENDING = "E_RECOMPUTE_PENDING"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_NON_FINITE_INPUT = "E_NON_FINITE_INPUT"
E_LENGTH_MISMATCH = "E_LENGTH_MISMATCH"
E_INVALID_CONFIG = "E_INVALID_CONFIG"
E_KERNEL_SCORE = "E_KERNEL_SCORE"
E_KERNEL_NORMALIZE = "E_KERNEL_NORMALIZE"
E_KERNEL_WEIGHTED = "E_KERNEL_WEIGHTED"
E_KERNEL_UNKNOWN = "E_KERNEL_UNKNOWN"
E_EDIT_REJECTED = "E_EDIT_REJECTED"


class IncrementalAttentionError(Exception):
    """Base exception carrying a stable business error code."""

    code = "E_KERNEL_UNKNOWN"

    def __init__(self, message, code=None):
        super().__init__(message)
        if code is not None:
            self.code = code


class NonFiniteInputError(IncrementalAttentionError):
    code = E_NON_FINITE_INPUT


class LengthMismatchError(IncrementalAttentionError):
    code = E_LENGTH_MISMATCH


class InvalidConfigError(IncrementalAttentionError):
    code = E_INVALID_CONFIG


class KernelStageError(IncrementalAttentionError):
    """Raised inside a specific kernel stage via fault injection."""

    def __init__(self, stage, message):
        super().__init__(message, _STAGE_CODE.get(stage, E_KERNEL_UNKNOWN))
        self.stage = stage


class SnapshotStaleError(IncrementalAttentionError):
    code = E_SNAPSHOT_STALE


class EditRejectedError(IncrementalAttentionError):
    code = E_EDIT_REJECTED


_STAGE_CODE = {
    "score": E_KERNEL_SCORE,
    "normalize": E_KERNEL_NORMALIZE,
    "weighted": E_KERNEL_WEIGHTED,
}
