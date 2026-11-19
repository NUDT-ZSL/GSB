# 实际验证记录

## 环境

本次执行环境：

```text
OS: Linux 6.18.33.2 (WSL2 Ubuntu)
Python: 3.12.13
NumPy: 2.5.5
pytest: 9.2.0
进程池: multiprocessing spawn（与 Windows 启动方式一致）
```

当前执行机不是 Windows，因此未在原生 Windows 解释器上重复执行；代码路径显式使用 `spawn`，并通过真实子进程测试。Windows 复现命令见 `README.md`。

## 自动测试

执行：

```bash
.venv/bin/python -m pytest
```

实际结果：

```text
....................                                                     [100%]
20 passed in 9.41s
```

覆盖点：

- 训练/推理摘要重复执行逐字节一致；训练损失序列长度等于 `steps`。
- 固定 seed 样本、清单、NumPy 张量和张量审计清单可重复生成。
- spawn 并行结果与串行结果完整记录一致；注入异常归因到 `job_id`、业务码和阶段。
- 计划重复构建完全一致；计算、内存、槽位拒绝码均被覆盖。
- 推理权重共享在估算与决策中可见。
- 降级同时缩小 batch 和 iterations，记录字段、类别及归还量，并由独立基线验证。
- 账本多线程读取期间总数始终完整，失败任务释放资源后可核对。
- 快照整文件替换、重启加载、旧版本拒绝、校验和/JSON 损坏定位。
- 阶段锁冲突立即返回 `E_STAGE_BUSY`。
- 管线演示、降级任务、快照损坏、账本业务不一致、版本冲突和首个数值偏差检测。
- 控制台状态聚合包含拒绝码和 worker 异常码。

## 固定演示

执行：

```bash
.venv/bin/python -m mixedpipe.cli demo
```

演示样本配置结果：

```text
准入：7
拒绝：1
拒绝任务：job-008
拒绝业务码：E_COMPUTE_BUDGET
worker 异常：job-006 / E_INJECTED_FAULT / stage=score
并行分组：3 组，槽位峰值 3
计算：18,029,648 / 20,000,000
内存：888,960 / 8,388,608 bytes
阶段版本：1
计划快照校验和：951e604cb8fb4cf3302622979db7614f6748035a695ac9dde3eb4911972f575b
账本快照校验和：c455cd6a2d9e3e5b2490545d686fd1c0fe3ac1f4fd468e585020238e4ddd927e
结果快照校验和：83d198ae727b7a1af25c0e54a5aba58145449c3a729c59a99a91ddd9f4b902da
验证：通过
串行基线核对：6 个成功任务 + 1 个异常归因
```

验证器检查全部通过：

- `snapshot_versions`
- `plan_stage_version`
- `ledger_stage_version`
- `results_stage_version`
- `ledger_plan_checksum`
- `results_plan_checksum`
- `results_ledger_checksum`
- `ledger_admission_set`
- `ledger_allocations`
- `ledger_budget_totals`
- `results_admission_set`
- `manifest_membership`
- `serial_baseline_equivalence`

## 控制台

启动：

```bash
.venv/bin/python -m mixedpipe.cli serve --host 127.0.0.1 --port 8765
```

接口：

```text
GET  /api/state
GET  /api/health
POST /api/run
POST /api/validate
```

控制台仅使用本地确定性样本和本地 spawn 进程池；演示运行固定包含一次预算拒绝和一次 worker 异常。

本次 HTTP 冒烟结果：

```text
GET  /api/health   -> 200 {"ok":true}
GET  /api/state    -> 200，plan/ledger/results 均为同一版本
POST /api/validate -> 200，ok=true，6 个成功任务 + 1 个异常归因
GET  /             -> 200，控制台 HTML 正常返回
```
