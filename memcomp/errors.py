"""统一错误码与异常类型。"""

E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_WINDOW_DUP_ENTRY = "E_WINDOW_DUP_ENTRY"
E_CHAIN_CYCLE = "E_CHAIN_CYCLE"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_INCONSISTENT = "E_INCONSISTENT"
E_CONFIG_INVALID = "E_CONFIG_INVALID"


class MemError(Exception):
    """领域错误，携带稳定错误码与可选的定位信息（会话、行号）。"""

    def __init__(self, code, message, session_id=None, line_no=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.session_id = session_id
        self.line_no = line_no

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "session_id": self.session_id,
            "line_no": self.line_no,
        }
