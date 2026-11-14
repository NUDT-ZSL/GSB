"""统一异常类型。"""


class ConfigError(Exception):
    """配置 JSON 非法：字段缺失、类型错误、取值越界等。

    抛出前消息中应携带字段所在位置（JSON 指针风格路径）。
    """


class PatchLoadError(Exception):
    """单个补丁文件无法读取或无法解码（不中止整体流程，跳过该补丁）。"""
