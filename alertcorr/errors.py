"""业务错误码定义。

每类非法输入映射到唯一错误码, 并保留可诊断上下文字段。
抛出 BusinessError 的路径保证不写入任何持久化状态。
"""


class BusinessError(Exception):
    def __init__(self, code, message, **context):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = context

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}


E1001_DUPLICATE_SERVICE = "E1001"
E1002_DUPLICATE_INSTANCE = "E1002"
E1003_UNKNOWN_UPSTREAM = "E1003"
E1004_SELF_DEPENDENCY = "E1004"
E1005_DEPENDENCY_CYCLE = "E1005"
E1006_INSTANCE_NOT_DECLARED = "E1006"
E1007_BAD_WEIGHT = "E1007"
E1008_BAD_TOPOLOGY_SHAPE = "E1008"

E2001_SEQ_NOT_INCREASING = "E2001"
E2002_NEGATIVE_CLOCK = "E2002"
E2003_INVALID_SEVERITY = "E2003"
E2004_INVALID_FLAG = "E2004"
E2005_NON_FINITE_METRIC = "E2005"
E2006_UNKNOWN_INSTANCE = "E2006"
E2007_BAD_EVENT_SHAPE = "E2007"

E3001_PATCH_UNKNOWN_SERVICE = "E3001"
E3002_INVALID_WINDOW_RANGE = "E3002"
E3003_TOPOLOGY_VERSION_ROLLBACK = "E3003"
E3004_PATCH_UNKNOWN_INSTANCE = "E3004"
E3005_PATCH_BASE_VERSION_MISMATCH = "E3005"
E3006_UNKNOWN_WINDOW = "E3006"
E3007_BAD_PATCH_SHAPE = "E3007"
