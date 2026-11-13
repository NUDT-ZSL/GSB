# -*- coding: utf-8 -*-
"""路径穿越（Path Traversal）静态检测与模拟利用工具包。

模块划分：
- rules:     污点源 / 危险汇聚点(sink) / 净化函数 规则定义
- detector:  基于 AST 的函数内污点分析扫描器
- simulator: 隔离沙箱内的恶意上传模拟利用器
- reporter:  JSON 报告、文本摘要、修复前后对比
- cli:       命令行入口
"""

__version__ = "1.0.0"
