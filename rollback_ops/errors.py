"""统一业务错误码与异常类型。"""

E_FORMAT = "E_FORMAT"      # 变更单格式/字段错误
E_DEPEND = "E_DEPEND"      # 回滚依赖图错误（重复、缺引用、成环、删除被依赖步骤）
E_VERSION = "E_VERSION"    # 版本冲突（账本版本、单调性）
E_INCR = "E_INCR"          # 增量重算与全量重算不一致
E_LEDGER = "E_LEDGER"      # 账本与回滚计划步骤不对应
E_REPLAY = "E_REPLAY"      # 恢复后重放校验不可复现

ALL_CODES = (E_FORMAT, E_DEPEND, E_VERSION, E_INCR, E_LEDGER, E_REPLAY)


class OpsError(Exception):
    """携带业务错误码与结构化细节的异常。"""

    def __init__(self, code, message, details=None):
        if code not in ALL_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}

    def render(self):
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
