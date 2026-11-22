"""Stable error codes for the index pipeline."""


class PipelineError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "E_PIPELINE"

    def __init__(self, message="", **details):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def __str__(self):
        if self.details:
            kv = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
            return f"{self.code}: {self.message} ({kv})"
        return f"{self.code}: {self.message}"


class ConfigInvalidError(PipelineError):
    code = "E_CONFIG_INVALID"


class ScoreMismatchError(PipelineError):
    code = "E_SCORE_MISMATCH"

    def __init__(self, query_id, doc_id, delta, **details):
        super().__init__(
            f"score mismatch at query={query_id} doc={doc_id} delta={delta}",
            query_id=query_id, doc_id=doc_id, delta=delta, **details,
        )
        self.query_id = query_id
        self.doc_id = doc_id
        self.delta = delta


class SnapshotRollbackError(PipelineError):
    code = "E_SNAPSHOT_ROLLBACK"


class SnapshotCorruptError(PipelineError):
    code = "E_SNAPSHOT_CORRUPT"

    def __init__(self, stage, version, reason="", **details):
        super().__init__(
            f"corrupt snapshot stage={stage} version={version}: {reason}",
            stage=stage, version=version, reason=reason, **details,
        )
        self.stage = stage
        self.version = version


class BuildBusyError(PipelineError):
    code = "E_BUILD_BUSY"


class ResourceLimitError(PipelineError):
    code = "E_RESOURCE_LIMIT"


class InconsistentError(PipelineError):
    code = "E_INCONSISTENT"

    def __init__(self, category, location, **details):
        super().__init__(
            f"inconsistency category={category} first_deviation={location}",
            category=category, location=location, **details,
        )
        self.category = category
        self.location = location
