"""GSB 主从复制延迟诊断与归档保留水位裁决引擎。

仅依赖 Python 标准库。包内模块：

- ``errors``      业务错误码与异常
- ``canon``       规范化 JSON / 摘要
- ``fixedpoint``  定点标度、舍入、余量结转与误差上界
- ``clock``       可注入逻辑时钟
- ``model``       样本解析与业务校验
- ``merge``       v1/v2 策略跨版本合并
- ``delay``       从库复制延迟与逐实例余量
- ``judge``       追赶余量、精度敏感与段归档裁决
- ``patch``       补丁影响闭包与快照整体替换
- ``store``       SQLite 事务持久化与崩溃恢复
- ``locking``     文件锁
- ``engine``      编排 load/merge/judge/resume/status/verify
- ``cli``         命令行入口
"""

__version__ = "1.0.0"
