# Verification Record

验证日期：2026-11-27  
环境：WSL Ubuntu，Python 3.10.12，NumPy 2.0.0，SQLite（Python 标准库）  
主种子：`20261127`

## 命令

```bash
python -m pip install numpy==2.0.0
python benchmark.py demo
python -m unittest discover -s tests -v
```

## 全链路演示结果

`python benchmark.py demo` 退出码为 `0`，实测耗时 2.14 秒，报告写入 `artifacts/demo/verification_report.json`。

基线 `demo-baseline`：

- 四阶段检查点版本：`1, 2, 3, 4`
- 已完成批次：`b-001`, `b-002`, `b-003`, `b-004`
- 账本条目：4
- 失败条目：0
- 账本 SHA-256：`bc916a92f1fc6b3926725477d3d9f12a80f4856903288d55d4dffea88503e56b`

无缓存独立复现 `demo-repro`：

- 账本字节等价：`true`
- 复现账本 SHA-256：`bc916a92f1fc6b3926725477d3d9f12a80f4856903288d55d4dffea88503e56b`

崩溃恢复 `demo-recovery`：

- 在 `aggregate` 提交后注入崩溃
- 重启从 `normalize`, `quantize`, `aggregate` 三个已提交检查点继续
- 丢弃 2 个未提交/未引用目录
- 仅补算 `spectrum`，检查点版本仍为 `1, 2, 3, 4`
- 恢复账本校验和与基线一致
- 逐项验证 24 个数组产物、4 条账本记录、单调版本、行级校验和均通过

快照迁移：

- v1 -> v2：活动集合、失效集合、毫秒边界保持 `true`
- v2 -> v1：活动集合、失效集合、毫秒边界保持 `true`；1 个墓碑原因按规则丢弃
- v2 -> v1 -> v2：活动集合、失效集合、毫秒边界保持 `true`

定向失效：

- 受影响任务：仅 `task-alpha`
- 受影响特征：`f0` 至 `f7`
- 计划复用 12 项，重算 4 项
- 实际运行复用 12 项，仅重算 `task-alpha` 的 `aggregate` 与 `spectrum` 后缀
- `task-beta` 四个阶段全部复用

快照提交控制：

- 锁冲突立即观测到 `E_STAGE_BUSY`
- 同版本不同校验和触发 `whole_snapshot_atomic_replace`
- 活动快照校验和等于新提交内容
- 版本倒退回 v1 观测到 `E_SNAPSHOT_ROLLBACK`

演示中还实际观测到：

- `E_DUP_TASK`
- `E_BAD_BATCH`
- `E_PARAM_INVALID`
- `E_REVISION_ROLLBACK`
- `E_CLOCK_ROLLBACK`

## 自动化测试

```text
Ran 12 tests in 1.388s
OK
```

测试还覆盖以下未在演示主路径中触发的失败分支：

- `E_QUANT_INPUT`：NaN 输入及首个非有限位置
- `E_QUANT_LAYER`：量化/反量化形状不符
- `E_BLOCK_MISMATCH`：首个偏差特征对、块号和位置
- `E_TOLERANCE_EXCEEDED`：严格容差下 4 条账本全部保留，2 个任务继续处理
- `E_VERSION_UNSUPPORTED`
- `E_FORMAT_CORRUPT`：缺字段、时间戳倒退
- `E_STAGE_BUSY`、`E_SNAPSHOT_ROLLBACK`、`E_CLOCK_ROLLBACK`

## 可检查产物

- `artifacts/demo/verification_report.json`
- `artifacts/demo/invalidation_plan.json`
- `artifacts/runs/demo-baseline/error_ledger.jsonl`
- `artifacts/runs/demo-recovery/error_ledger.jsonl`
- `artifacts/runs/demo-revision/error_ledger.jsonl`
- `artifacts/cache/index.json`
- `artifacts/snapshots/active_snapshot.json`
