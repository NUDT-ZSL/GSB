# 实际验证报告

验证日期：2026-11-22  
运行环境：Python 3.11.18，SQLite 3.53.1，Ubuntu 22.04（本地，无容器、无网络）

Windows 互斥路径使用标准库 `msvcrt.locking`，进程启动使用 `spawn`；本次实际测试运行在
WSL/Ubuntu 环境，原生 Windows 分支未在另一台主机上重复执行。

复跑命令：

```bash
python3.11 scripts/run_acceptance.py
```

机器可读结果：`artifacts/acceptance-report.json`  
完整测试日志：`artifacts/test-output.txt`

## 测试结果

```text
Ran 32 tests in 7.985s
OK
```

覆盖范围：

- 非法脚本错误码与零运行文件副作用
- 表/列唯一性、枚举闭合、预算/重试/标度合法性
- 共享锁兼容、重复申请复用、FIFO 不插队
- 范围重叠冲突与显式锁声明
- 逻辑时钟等待、定点余量、误差来源链
- 等待预算超时、持有锁释放、主动回滚
- 事务累计等待预算耗尽（不按单次等待重置）
- 死锁环识别、牺牲依据、敏感标记与高标度翻转
- 单进程、重复运行、双进程结果一致性
- `enqueue`、`grant`、`abandon` 三个中途强制终止点恢复
- 两个 `resume` 实例并发提交的幂等性
- 数据库触发器阻止同资源并发超授
- 状态命令稳定排序、`verify` 真值与不变量

## 主样本结果

账本摘要：

```text
61c5cdd5cae27bb7425a7f025a3990bf5bd827018ebcb3648e8dedffb19e01d3
```

等待登记序列：

| 登记序号 | 事务 | 原始等待 | 固定点 | 精确值 | 余量 | 结局 |
|---:|---|---:|---:|---:|---:|---|
| 1 | `T08_RANGE_WRITER` | 1 ms | 0 | 2/5 | 2/5 | GRANTED |
| 2 | `T07_INVENTORY_WAITER` | 2 ms | 0 | 4/5 | 4/5 | GRANTED |
| 3 | `T02_TIMEOUT` | 2 ms | 0 | 4/5 | 4/5 | TIMEOUT |
| 4 | `T03_DEADLOCK_LOW_TXNO` | 1 ms | 0 | 2/5 | 2/5 | SACRIFICED |
| 5 | `T04_DEADLOCK_HIGH_TXNO` | 2 ms | 0 | 4/5 | 4/5 | GRANTED |

结局：

- `T02_TIMEOUT` 在 2 ms 预算到期时中止，释放此前持有的 `row:jobs:7` 排他锁。
- `T09_ACTIVE_ROLLBACK` 完成 1 个操作后主动回滚，标记 `ABORTED / VOLUNTARY_ROLLBACK`。
- `T03`/`T04` 构成完整死锁环。当前牺牲者为 `T03`，等待 1 ms，偏差与误差上界均为 `2/5`。
- 敏感判定确认当前两者固定点均为 0；标度提高 1000 倍后牺牲者翻转为等待 2 ms 的 `T04`。
- 完成后 `lock_holders` 为空，队列无 `WAITING` 项。

## 并发与恢复

| 验证 | 结果 |
|---|---|
| 两次独立单进程运行 | 摘要和等待登记序列逐位一致 |
| 双进程 `--workers 2` | 摘要与单进程一致，等待登记序列一致 |
| 崩溃点 `enqueue@tick1` | 退出码 137，水位 0，恢复后摘要一致 |
| 崩溃点 `grant@tick0` | 未提交授予整体回滚，恢复后摘要一致 |
| 崩溃点 `abandon@tick3` | 未提交超时/放弃整体回滚，恢复后摘要一致 |
| 两个并发 `resume` | 最终账本摘要一致，无重复登记 |
| `verify` | 精确真值、账本摘要重算、隔离目录串行重放全部通过；无残留锁和等待队列项 |

