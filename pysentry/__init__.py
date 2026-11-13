"""pysentry - 面向内部模板批量展开服务的离线静态检测工具。

仅依赖 Python 标准库，对 .py 文件做单文件 AST 分析，不跨文件追踪符号。
"""

__version__ = "1.0.0"

RULE_IDS = ("SEC001", "SEC002", "SEC003")
