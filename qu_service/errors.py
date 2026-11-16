"""业务码与异常定义。"""

# 业务码
E_EMPTY_QUERY = "E_EMPTY_QUERY"            # 空查询或仅含标点
E_MALFORMED_QUERY = "E_MALFORMED_QUERY"    # 注入的畸形查询
E_SLOT_OVERLAP = "E_SLOT_OVERLAP"          # 槽位候选重叠被抑制
E_RULE_MISSING = "E_RULE_MISSING"          # 改写规则引用缺失
E_RULE_CONFLICT = "E_RULE_CONFLICT"        # 注入的规则冲突被消解
E_SCORE_OUT_OF_RANGE = "E_SCORE_OUT_OF_RANGE"  # 注入的评分越界被截断
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"    # 快照配置/注入哈希不符
E_INTERNAL = "E_INTERNAL"                  # 未预期异常


class StageError(Exception):
    """阶段内可预期业务异常，携带业务码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ForcedTermination(Exception):
    """模拟强制终止：仅用于实验台验证快照恢复链路。"""


class GracefulExit(Exception):
    """收到可处理退出信号：当前阶段快照已持久化，以退出码 0 结束。"""


class ConfigMismatch(Exception):
    """恢复时配置或注入配置哈希不符。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.code = E_CONFIG_MISMATCH
