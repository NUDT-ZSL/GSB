# 架构与设计说明

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `orchestrator/dag_loader.py` | DAG JSON 加载、字段级强校验、无环检测、DAG+脚本指纹 |
| `orchestrator/models.py` | 阶段、依赖、尝试、轨迹事件、快照等不可变/数据类模型 |
| `orchestrator/scheduler.py` | 资源约束并行调度、状态机、重试、失败传播、快照持久化 |
| `orchestrator/executor.py` | 本地 Python 子进程执行、超时、stderr 换行标准化、SHA-256 摘要 |
| `orchestrator/snapshot.py` | 原子快照读写、版本识别、整体替换、硬崩溃仅恢复已完成阶段 |
| `orchestrator/verifier.py` | 独立验证：依赖顺序、资源预算审计、摘要一致性、skip 依据 |
| `orchestrator/replay.py` | 恢复后按拓扑序重放已完成阶段并比对字节摘要 |
| `orchestrator/view_model.py` | 与 Tk 解耦的稳定视图模型（无显示环境可无头运行） |
| `orchestrator/ui.py` | Tkinter 五个标签页渲染与无头降级入口 |
| `orchestrator/cli.py` | `validate-dag/run/verify/replay/recover/ui` 统一命令行 |

## 状态机

```
pending -> ready -> running -> succeeded
                     |          |
                     v          v
                   failed    (摘要落盘)
强依赖 failed/skipped: pending -> skipped (记录 skipped_by)
弱依赖 failed/skipped: 下游仍 running -> succeeded, 产出标注 missing_upstreams
硬崩溃后未完成阶段:    标记 unknown，要求重跑，不生成退出报告
```

同一阶段仅在实际取得进程结果时迁移一次终态，成功事件按阶段去重，
不会产生重复成功记录。

## 资源与并发

- 全局资源预算：每次启动前判断 `used + request <= budget`，否则进入等待队列并记录等待开始时刻。
- 每阶段并发配额：`stage.max_concurrency`，缺省取 `default_max_concurrency`。
- 全局并发闸门：在跑阶段数达到 `default_max_concurrency` 时新候选进入等待。
- 等待轨迹区分 `resource_budget_unavailable`、`stage_concurrency_quota:n`、
  `global_concurrency_quota:n` 三类触发原因。
- 验证器按轨迹重建资源占用曲线，断言峰值不超过预算、记账不为负。

## 子进程协议

调度器通过环境变量与阶段脚本通信，避免命令行长度与跨平台引号问题：

| 变量 | 含义 |
| --- | --- |
| `PIPE_STAGE_ID` | 阶段标识 |
| `PIPE_OUTPUT_PATH` | 该次运行的输出 JSONL 绝对路径 |
| `PIPE_INPUT_MAP` | 上游阶段标识 -> 成功产出路径的 JSON 映射 |
| `PIPE_MISSING_UPSTREAMS` | 失败/跳过的弱依赖标识列表 |
| `PIPE_ATTEMPT_NO` | 第几次尝试（从 1 开始） |
| `PIPE_FAIL_FIRST_N` | 确定性失败注入配置 |

脚本目录通过 `PYTHONPATH` 注入，标准错误统一把 CRLF/CR 归一为 LF，
输出文件按行读取并计算 SHA-256 与 JSONL 记录数。

## 业务码

| 业务码 | 触发场景 | CLI 退出码 |
| --- | --- | --- |
| `E_DAG` | 标识重复、依赖缺失、环、正整数字段非法等 | 2 |
| `E_VERIFY` | 依赖顺序/预算/摘要/skip 依据/终态覆盖偏差 | 3 |
| `E_REPLAY` | 指纹漂移或重算摘要不一致 | 4 |
| `E_SNAPSHOT` | 快照缺失、损坏或格式版本不支持 | 5 |
| `E_EXEC` | 执行层结构性错误 | 6 |

## 快照与恢复

- 写入采用临时文件 + `os.replace` 原子替换，键排序保证可 diff。
- 快照保存 DAG 版本、DAG+脚本指纹、预算、轨迹、状态、摘要、已完成集合。
- 重新打开时比较版本与指纹；不一致执行整体替换策略，不做增量合并。
- 硬崩溃恢复只信任 `completed_stages` 中状态为 `succeeded` 的阶段，
  其余阶段标记 `unknown` 并要求重跑，不承诺退出报告。
- 重放先做指纹预检查，再按拓扑序在独立目录重算，逐项比较 SHA-256。

## Tkinter 无头降级

`view_model.build_dashboard_model` 是纯函数，不依赖 Tk。容器或 CI 无显示时
使用 `python -m orchestrator.cli ui --snapshot <file> --headless` 即可得到
与界面相同排序的数据；Windows 标准 Python 自带 Tk，直接打开图形界面。
