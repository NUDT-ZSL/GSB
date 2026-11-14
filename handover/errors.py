"""自定义异常类型。"""

from __future__ import annotations


class HandoverError(Exception):
    """本工具所有可预期错误的基类。"""


class ConfigError(HandoverError):
    """映射配置格式错误 / 引用了不存在的规范字段等。

    message 必须包含具体的键名或位置，便于定位。
    """


class PipelineError(HandoverError):
    """运行期致命错误（如输入目录不存在）。"""
