"""Business error codes and the pipeline exception type."""

E_DUP_PAIR = "E_DUP_PAIR"
E_LANG_UNSUPPORTED = "E_LANG_UNSUPPORTED"
E_MAP_FORMAT = "E_MAP_FORMAT"
E_GRADE_INVALID = "E_GRADE_INVALID"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_INPUT_PARSE = "E_INPUT_PARSE"
E_IO_WRITE = "E_IO_WRITE"


class PipelineError(Exception):
    """Business error carrying stage / pair / resource attribution."""

    def __init__(self, code, message, *, stage=None, pair_id=None,
                 resource=None, version=None, category=None):
        self.code = code
        self.stage = stage
        self.pair_id = pair_id
        self.resource = resource
        self.version = version
        self.category = category
        super().__init__(message)

    def to_dict(self):
        return {
            "code": self.code,
            "message": str(self),
            "stage": self.stage,
            "pair_id": self.pair_id,
            "resource": self.resource,
            "version": self.version,
            "category": self.category,
        }
