"""多来源 JSONL 业务记录交接规范化工具包。

模块划分：
- config:   映射配置加载与严格校验
- tzutil:   时区解析与日期规范化
- reader:   单文件读取（锁占用 / 截断 / 坏行识别）
- pipeline: 映射、必填校验、去重合并、冲突裁决、状态续跑
- report:   交接报告（JSON + Markdown）
- cli:      命令行入口
"""

__version__ = "1.0.0"
