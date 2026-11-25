"""Structured business error codes for the match-verification component."""

E_DUP_IMAGE = "E_DUP_IMAGE"
E_BAD_BOX = "E_BAD_BOX"
E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_MIGRATION_MISMATCH = "E_MIGRATION_MISMATCH"
E_REPLAY_MISMATCH = "E_REPLAY_MISMATCH"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_THRESHOLD_RANGE = "E_THRESHOLD_RANGE"
E_REVISION_REGRESS = "E_REVISION_REGRESS"
E_CONFIG_INVALID = "E_CONFIG_INVALID"


class BusinessError(Exception):
    """Structured business error carrying a stable machine-readable code."""

    def __init__(self, code, message, field=None, context=None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.field = field
        self.context = context or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "context": self.context,
        }
