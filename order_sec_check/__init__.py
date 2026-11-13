"""订单查询接口安全自测工具（仅使用 Python 标准库）。

模块划分：
- config:        用例 JSON 的结构校验（字段缺失/类型错误即终止）
- netpolicy:     仅允许访问本地回环地址
- redact:        凭据脱敏
- validate:      业务参数合规校验
- detector:      HTTP 检测与命中判定规则引擎
- report:        结构化报告写入（同名文件不覆盖）
- logging_setup: 可读日志
- cli:           命令行入口
"""

__version__ = "1.0.0"
