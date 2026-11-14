"""统一的可读异常类型。

CLI 只捕获 ContactSheetError，向 stderr 打印中文原因并以非零码退出；
任何 ContactSheetError 都在写文件之前抛出，因此不会留下半成品联系表。
"""


class ContactSheetError(Exception):
    """所有可预期的业务失败（配置非法、视频不可解码等）。"""


class ConfigError(ContactSheetError):
    """联系表配置缺失、字段类型错误或尺寸越界。"""


class VideoReadError(ContactSheetError):
    """视频文件不存在、无法探测或帧解码失败。"""
