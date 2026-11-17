"""注入类漏洞静态检测工作台。

模块划分：
- config:      版本、阈值与资源预算配置
- transforms:  编码变换注册表与编码族/方向归一
- parser:      清单解析与记录级校验（拒绝记录）
- analyzer:    规范链归一、路径缺陷判定、汇点聚合与规模模式
- resources:   墙钟超时、路径数预算与文件锁
- checkpoints: 检查点持久化与版本变化重建
- report:      JSON 报告与批处理编排
- cli / webapp:命令行与本地诊断工作台
"""

__version__ = "1.0.0"
