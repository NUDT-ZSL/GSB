"""工具内统一异常类型。"""


class ConfigError(ValueError):
    """场景配置非法（字段类型/取值范围/约定上限），必须整体拒绝处理。"""
