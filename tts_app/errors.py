"""业务错误码与异常定义。"""

E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"

ALL_CODES = (
    E_VERSION_UNSUPPORTED,
    E_FORMAT_CORRUPT,
    E_SEMANTIC_DRIFT,
    E_LINEAGE_BROKEN,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_RESOURCE_LIMIT,
)


class TtsError(Exception):
    """携带业务错误码与结构化详情的异常。"""

    def __init__(self, code, message, **details):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
