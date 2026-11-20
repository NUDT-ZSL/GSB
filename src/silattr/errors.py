"""业务错误码与领域异常。"""

E_OK = "E_OK"
E_WINDOW_INVALID = "E_WINDOW_INVALID"
E_SEGMENT_SPILL = "E_SEGMENT_SPILL"
E_SOURCE_UNKNOWN = "E_SOURCE_UNKNOWN"
E_RANGE_OUT = "E_RANGE_OUT"
E_OVERLAP = "E_OVERLAP"
E_INCONSISTENT = "E_INCONSISTENT"
E_INCREMENT_MISMATCH = "E_INCREMENT_MISMATCH"
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_DUP_CALL = "E_DUP_CALL"
E_FRAME_COUNT_MISMATCH = "E_FRAME_COUNT_MISMATCH"
E_PROFILE_MISSING = "E_PROFILE_MISSING"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"

BUSINESS_CODES = {
    E_OK: "成功",
    E_WINDOW_INVALID: "噪声底估计窗口非法（样本数不足或步长为零）",
    E_SEGMENT_SPILL: "噪声底分段跨越语音活动帧",
    E_SOURCE_UNKNOWN: "归因类别不在受控集合",
    E_RANGE_OUT: "事件时间轴越界",
    E_OVERLAP: "事件时间轴存在重叠",
    E_INCONSISTENT: "画像与逐事件明细重算不一致",
    E_INCREMENT_MISMATCH: "增量重算与全量重算结果不一致",
    E_LINEAGE_BROKEN: "归因血缘缺失或重放不符",
    E_SNAPSHOT_CORRUPT: "快照损坏或校验和不符",
    E_STAGE_BUSY: "估计快照提交冲突或旧版本回退",
    E_DUP_CALL: "重复 call_id",
    E_FRAME_COUNT_MISMATCH: "duration_ms 与帧级观测帧数不符",
    E_PROFILE_MISSING: "noise_profile_ref 缺失",
    E_RESOURCE_LIMIT: "超出资源上限",
}


class DomainError(Exception):
    """携带业务码与结构化上下文的领域异常。"""

    def __init__(self, code, message="", **context):
        self.code = code
        self.context = context
        detail = BUSINESS_CODES.get(code, code)
        full = f"{code}: {message or detail}"
        if context:
            full += f" | context={context}"
        super().__init__(full)

