"""整数业务码与业务异常定义。

所有业务失败均以 :class:`BusinessError` 抛出，``code`` 为稳定的整数业务码，
``field_path`` 定位到具体清单字段，保证诊断输出可重复、可比较。
"""

OK = 0
E_MANIFEST = 4001
E_ESTIMATE = 4002
E_QUOTA = 4003
E_PLACEMENT = 4004
E_SNAPSHOT = 4005
E_REPLAY = 4006

CODE_NAMES = {
    OK: "OK",
    E_MANIFEST: "E_MANIFEST",
    E_ESTIMATE: "E_ESTIMATE",
    E_QUOTA: "E_QUOTA",
    E_PLACEMENT: "E_PLACEMENT",
    E_SNAPSHOT: "E_SNAPSHOT",
    E_REPLAY: "E_REPLAY",
}


def code_name(code):
    return CODE_NAMES.get(code, "E_UNKNOWN")


class BusinessError(Exception):
    """带整数业务码与字段位置的业务异常。"""

    def __init__(self, code, message, field_path=None, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field_path = field_path
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "code_name": code_name(self.code),
            "message": self.message,
            "field_path": self.field_path,
            "details": self.details,
        }


class ManifestValidationError(BusinessError):
    """清单强校验聚合异常，收集全部字段级错误后整体拒绝加载。"""

    def __init__(self, issues):
        self.issues = list(issues)
        detail = "; ".join(
            "{}: {}".format(item["field_path"], item["message"]) for item in self.issues
        )
        super().__init__(
            E_MANIFEST,
            "manifest validation failed with {} issue(s): {}".format(len(self.issues), detail),
            field_path="$",
            details={"issues": self.issues},
        )
