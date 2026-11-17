"""业务异常与业务码定义。"""

E_INPUT_INVALID = "E_INPUT_INVALID"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_STAGE_FAILED = "E_STAGE_FAILED"
E_SIGNALLED = "E_SIGNALLED"


class RankerError(Exception):
    """所有精排组件异常的基类，携带业务码与发生阶段。"""

    code = E_STAGE_FAILED

    def __init__(self, message, stage=None, code=None):
        super().__init__(message)
        self.stage = stage
        if code is not None:
            self.code = code


class InputInvalid(RankerError):
    code = E_INPUT_INVALID


class ConfigInvalid(RankerError):
    code = E_CONFIG_INVALID


class ConfigMismatch(RankerError):
    code = E_CONFIG_MISMATCH


class StageFailed(RankerError):
    code = E_STAGE_FAILED
