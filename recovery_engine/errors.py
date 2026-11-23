"""业务错误码与异常类型。

每种异常链路映射到独立错误码，并保留可诊断字段（details）。
"""


class ErrCode:
    E_BAD_CONFIG = "E_BAD_CONFIG"                # 配置非法（通用）
    E_CYCLE = "E_CYCLE"                          # 依赖成环
    E_HASH_MISMATCH = "E_HASH_MISMATCH"          # 分片哈希不匹配
    E_GENERATION_CONFLICT = "E_GENERATION_CONFLICT"  # 配置代数冲突
    E_CLOCK_REGRESS = "E_CLOCK_REGRESS"          # 逻辑时钟回拨超容差
    E_CONN_REFUSED = "E_CONN_REFUSED"            # 动作目标连接拒绝
    E_BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"      # 动作超出单次逻辑预算
    E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"    # 配置包版本回退
    E_TOMBSTONED = "E_TOMBSTONED"                # 服务已打失效墓碑
    E_UPSTREAM_NOT_READY = "E_UPSTREAM_NOT_READY"  # 上游未就绪


class BizError(Exception):
    """业务异常：code 为稳定错误码，details 保留可诊断字段。"""""

    def __init__(self, code, message, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
