"""业务错误码：非法迁移、异常序号、重放不一致、版本不一致、时钟异常。"""

E_STATE = "E_STATE"
E_SEQ = "E_SEQ"
E_REPLAY = "E_REPLAY"
E_VERSION = "E_VERSION"
E_CLOCK = "E_CLOCK"


class BizError(Exception):
    """携带业务码的异常，界面与演示脚本按 code 分类展示。"""

    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
