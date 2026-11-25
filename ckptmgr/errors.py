"""Business errors and stable CLI exit codes."""

from typing import Any, Dict


class CheckpointError(Exception):
    """An expected business failure with a stable machine-readable code."""

    code = "E_CHECKPOINT"
    exit_code = 1

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: Dict[str, Any] = details

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class UsageError(CheckpointError):
    code = "E_USAGE"
    exit_code = 2


class PlanValidationError(CheckpointError):
    code = "E_PLAN_INVALID"
    exit_code = 2


class PlanFieldError(PlanValidationError):
    def __init__(self, field: str, message: str, **details: Any) -> None:
        super().__init__(message, field=field, **details)
        self.code = "E_PLAN_INVALID"


class LayerNameDuplicateError(PlanValidationError):
    code = "E_PLAN_LAYER_NAME_DUPLICATE"


class ShapeInvalidError(PlanValidationError):
    code = "E_PLAN_SHAPE_INVALID"


class DtypeInvalidError(PlanValidationError):
    code = "E_PLAN_DTYPE_INVALID"


class ShardCountInvalidError(PlanValidationError):
    code = "E_PLAN_SHARD_COUNT_INVALID"


class StrategyInvalidError(PlanValidationError):
    code = "E_PLAN_STRATEGY_INVALID"


class VersionOrderInvalidError(PlanValidationError):
    code = "E_PLAN_VERSION_ORDER_INVALID"


class ShardSizeExceededError(PlanValidationError):
    code = "E_SHARD_SIZE_EXCEEDED"


class ManifestValidationError(CheckpointError):
    code = "E_MANIFEST_INVALID"
    exit_code = 2


class ManifestDigestMismatchError(CheckpointError):
    code = "E_MANIFEST_DIGEST_MISMATCH"
    exit_code = 4


class ShardMissingError(CheckpointError):
    code = "E_SHARD_MISSING"
    exit_code = 3


class ShardDigestMismatchError(CheckpointError):
    code = "E_SHARD_DIGEST_MISMATCH"
    exit_code = 4


class ShardFormatError(CheckpointError):
    code = "E_SHARD_FORMAT_INVALID"
    exit_code = 4


class StitchOrderMismatchError(CheckpointError):
    code = "E_STITCH_ORDER_MISMATCH"
    exit_code = 5


class ParameterDigestMismatchError(CheckpointError):
    code = "E_PARAMETER_DIGEST_MISMATCH"
    exit_code = 6


class LayerShapeMismatchError(CheckpointError):
    code = "E_LAYER_SHAPE_MISMATCH"
    exit_code = 7


class RebuildMismatchError(CheckpointError):
    code = "E_REBUILD_MISMATCH"
    exit_code = 8


class VersionRollbackError(CheckpointError):
    code = "E_VERSION_ROLLBACK"
    exit_code = 9


class ClockRegressionError(CheckpointError):
    code = "E_CLOCK_REGRESSION"
    exit_code = 10


class CheckpointNotFoundError(CheckpointError):
    code = "E_CHECKPOINT_NOT_FOUND"
    exit_code = 11


class CheckpointIncompleteError(CheckpointError):
    code = "E_CHECKPOINT_INCOMPLETE"
    exit_code = 12


class CheckpointConflictError(CheckpointError):
    code = "E_CHECKPOINT_CONFLICT"
    exit_code = 13


class RoundBudgetExhaustedError(CheckpointError):
    code = "E_ROUND_BUDGET_EXHAUSTED"
    exit_code = 14


class ShardWriteFailedError(CheckpointError):
    code = "E_SHARD_WRITE_FAILED"
    exit_code = 15


class PlanDigestMismatchError(CheckpointError):
    code = "E_PLAN_DIGEST_MISMATCH"
    exit_code = 16


class VerificationFailedError(CheckpointError):
    code = "E_VERIFY_FAILED"
    exit_code = 17


class ReuseRecordInvalidError(CheckpointError):
    code = "E_REUSE_RECORD_INVALID"
    exit_code = 18


class InternalError(CheckpointError):
    code = "E_INTERNAL"
    exit_code = 70
