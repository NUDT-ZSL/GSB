"""业务错误码定义。"""

E_SCHEMA_UNKNOWN = "E_SCHEMA_UNKNOWN"      # schema 版本无法识别
E_UNIT_MISSING = "E_UNIT_MISSING"          # 单位字段缺失
E_UNIT_UNKNOWN = "E_UNIT_UNKNOWN"          # 单位取值无法识别
E_SCHEMA_STRUCTURE = "E_SCHEMA_STRUCTURE"  # 文档结构与声明的 schema 不符
E_VALIDATION = "E_VALIDATION"              # 数据校验失败
E_CYCLE = "E_CYCLE"                        # 工序依赖成环
E_EVENT = "E_EVENT"                        # 反馈事件非法
E_NOT_FOUND = "E_NOT_FOUND"                # 查询对象不存在


class BusinessError(Exception):
    """携带业务错误码的异常，一律不猜测、不静默兜底。"""

    def __init__(self, code, message, details=None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or {}
