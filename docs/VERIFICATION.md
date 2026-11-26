# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）。

## 环境

- Python `3.10.12`
- SQLite 标准库版本 `3.37.2`
- NumPy `2.0.0`
- 平台：`Linux ... microsoft-standard-WSL2-x86_64`，即 WSL2 Ubuntu 环境
- 未使用 Docker、外部服务或网络

## 自动化回归

命令：

```bash
python3 -m pytest tests/test_dataplane.py -v --tb=short
```

实际结果：

```text
collected 8 items
tests/test_dataplane.py ........                                         [100%]
============================== 8 passed in 9.13s ===============================
```

覆盖用例：

- 固定种子样本、生成、补丁闭包、裁决和严格校验端到端通过。
- 并发限制导致的 `not_started/concurrency_limit/within_budget`。
- 预算不足导致的 `abandoned/budget_exhausted`。
- `DP_FAIL_AT` 注入启动后失败，`resume` 重试且账本不重复。
- `materialized_committed` 后强杀，恢复半提交块。
- 配置崩溃 pending 恢复、增量分类、noop 和版本回退拒绝。
- 4 进程同时处理同一 case 的幂等物化。
- 八类输入/配置/时钟/代数错误码。

## 一键验收

命令：

```bash
bash scripts/acceptance.sh data
```

实际结果：退出码 `0`，末行：

```text
ACCEPTANCE_OK data_dir=data crash_dir=/tmp/dp-acceptance-crash-j8tS
```

验收后的持久化计数：

```text
cases=8
tensor_blocks=52
patches=9
block_tasks=82
inference_decisions=82
error_ledger=82
task_states={"committed": 82}
active_config_version=2026112703
classification=incremental_append
```

严格校验结果：

```text
ok=true
complete=true
objects_checked=82
deterministic_recomputations=82
ledger_rows_checked=82
issues=[]
batch.decision_count=26
batch.precision_sensitive_count=26
batch.total_error_bound=8.878503192703226
batch.total_error_units=4559
```

82 个任务包含初始配置 `2026112702` 的 41 个当前/修订任务，以及增量配置 `2026112703` 下重新规划的 41 个任务；每个任务都通过了从数据库描述符重新计算后的摘要比对。

## 确定性复现

在两个独立临时目录中执行相同的 `seed -> load -> generate -> patch -> verify`，初始配置下均得到：

```text
objects_checked=41
batch.decision_count=26
batch.total_error_bound=9.138188464664772
batch.total_error_units=4690
```

两个目录的 `objects/` 文件路径与文件摘要聚合值相同：

```text
e2df7d72bcfdde2339080948045c5e3543ab2d53f89784e80e605f0bff98d5bb  -
e2df7d72bcfdde2339080948045c5e3543ab2d53f89784e80e605f0bff98d5bb  -
OBJECT_TREE_IDENTICAL
```

## 多进程一致性

- 4 个 CLI 进程同时执行 `generate --case-id case-001`：最终 `block_task=3`、`inference_decision=3`、`error_ledger=3`，`verify --strict` 通过。
- 8 个 CLI 进程分别处理 8 个不同 case：最终 `block_task=26`、`inference_decision=26`、不同内容摘要数 `26`，严格校验通过。

## 强制终止恢复

已实际验证两个块崩溃点：

- `DP_CRASH_AT=materialized_committed:case-001:0`：CLI 以 `97` 退出；状态中存在 `materialized` 半提交块。`resume` 记录 `finalized_materialized`，第二次 `resume` 后 decision/error 数量不变，严格校验通过。
- `DP_CRASH_AT=object_written:case-001:0`：对象已原子落盘但数据库仍为 `running`。恢复获取块锁后将陈旧租约重置为 `pending` 并重放，严格校验通过。

配置替换崩溃：

- `DP_CRASH_AT=config_staged:2026112703` 后，旧指针仍为 `2026112702`，`pending_config=true`。
- `resume` 成功完成 pending 包，分类为 `incremental_append`。
- 再次安装同一包返回 `noop`；安装旧版本返回 `E_CONFIG_VERSION_ROLLBACK`。

## 调度可追溯性

固定样本初始共有 26 个算子块，单并发、单轮 `--max-rounds 1` 时：

```text
completed_count=1
not_started_count=25
unselected[0].reason=concurrency_limit
unselected[0].budget_state=within_budget
```

小预算场景会产生 `outcome=abandoned`、`reason=budget_exhausted` 的剩余候选；轮次表和 `round_item` 表保存剩余块数与剩余成本。
