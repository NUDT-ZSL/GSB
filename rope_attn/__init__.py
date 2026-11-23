"""RoPE 分块注意力算子组件（纯 NumPy / 本地文件持久化 / 本地进程池）。

公共入口：
    Task / RunConfig              任务与运行配置数据类
    validate_tasks                输入与配置校验链路
    ContentCache                  内容寻址缓存
    SnapshotStore                 阶段快照状态机
    Executor                      分层分块算子流水线执行器
    plan_kv_change / plan_block_size_change  增量失效传播
    run_self_check                跨模块一致性自检
    BusinessError 及其子类        结构化业务码异常
"""

from .config import RunConfig, Task, validate_tasks
from .errors import BusinessError
from .cache import ContentCache
from .state import SnapshotStore
from .pipeline import Executor, RunResult
from .nodes import plan_block_size_change, plan_kv_change
from .selfcheck import run_self_check

__all__ = [
    "Task",
    "RunConfig",
    "validate_tasks",
    "BusinessError",
    "ContentCache",
    "SnapshotStore",
    "Executor",
    "RunResult",
    "plan_kv_change",
    "plan_block_size_change",
    "run_self_check",
]

__version__ = "1.0.0"
