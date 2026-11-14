"""自定义异常定义。"""


class TrackValidationError(ValueError):
    """轨道数据校验失败。

    message 中必须包含出问题的字段名，便于命令行直接展示。
    """


class OcclusionValidationError(ValueError):
    """遮挡表数据校验失败（结构性错误，不含越界引用这类可继续处理的问题）。"""
