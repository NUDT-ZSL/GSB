"""业务错误码定义。

所有非法输入与运行时业务异常都映射为 BusinessError，携带稳定的数字错误码，
命令行以该错误码作为进程退出码，且出错时不写入任何持久化状态。
"""

from __future__ import annotations


class BusinessError(Exception):
    """携带业务错误码的异常。"""

    def __init__(self, code: int, message: str):
        super().__init__(f"E{code}: {message}")
        self.code = code
        self.message = message


# ---- 解析校验类 (1xxx) ----
E_TABLE_NAME_DUPLICATE = 1001
E_NDV_INVALID = 1002
E_COL_WIDTH_INVALID = 1003
E_PREDICATE_COLUMN_MISSING = 1004
E_PREDICATE_TYPE_INCOMPARABLE = 1005
E_BATCH_SEQ_NOT_INCREASING = 1006
E_BATCH_ROWS_NEGATIVE = 1007
E_CLOCK_NEGATIVE = 1008
E_ALGO_NOT_IN_ENUM = 1009
E_PLAN_TABLE_UNKNOWN = 1010
E_ROWS_INVALID = 1011
E_CONFIG_INVALID = 1012
E_INJECTION_MANIFEST_INVALID = 1013

# ---- 注入异常类 (2xxx)，生成/观测/切换三阶段各自独立 ----
E_DIGEST_MISMATCH = 2001          # 生成阶段：摘要不匹配
E_OBSERVATION_SAMPLE_ZERO = 2002  # 观测阶段：观测样本为零
E_SWITCH_BUDGET_ILLEGAL = 2003    # 切换阶段：切换预算非法

# ---- 运行时裁决类 (3xxx) ----
E_SWITCH_BUDGET_EXCEEDED = 3001   # 预算不足，保留原形态
E_FORM_VERSION_REGRESSION = 3002  # 形态版本回退被拒绝
E_BATCH_CONFLICT = 3003           # 同批次同版本重复提交但内容不同
E_STATE_NOT_LOADED = 3004         # 尚未 load 就执行运行类命令
E_TRUTH_MISMATCH = 3005           # 实测输出与真值不一致


def err(code: int, message: str) -> BusinessError:
    return BusinessError(code, message)
