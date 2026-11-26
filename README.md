# 消息投递语义可视化与诊断工作台

面向消息投递语义的确定性领域工作台：合并 v1/v2 快照、计算分区水位与依赖失效闭包、生成订阅增量补丁、调度投递诊断、持久化恢复，并提供稳定 JSON/Markdown CLI。

- Python 3.10+，仅使用 `sqlite3`、`json`、`argparse` 等标准库。
- 不使用 Docker，不访问网络或未授权目标。
- 所有样本和 SQLite 状态默认生成在当前工作目录。

## 快速开始

```bash
python3 seed_samples.py
python3 -m unittest discover -s tests -v

./workbench load
./workbench merge
./workbench diagnose
./workbench patch
./workbench status
./workbench status --format markdown
./workbench verify
```

也可以使用：

```bash
python3 workbench.py <command>
python3 -m delivery_workbench <command>
python3 seed_data.py
```

一键验收：

```bash
python3 scripts/run_acceptance.py
```

实际验证结果见 `docs/VERIFICATION.md`，技术设计见 `docs/TECHNICAL.md`，机器可读报告见 `artifacts/acceptance-report.json`。

## 生成的样本

`python3 seed_samples.py` 会确定性生成：

```text
samples/
├── topics.json
├── events.jsonl
├── subscriptions.json
├── semantics/
│   ├── v1_snapshot.json
│   ├── v2_manifest.json
│   ├── v2_metadata.sqlite
│   └── v2/shard-000.json, shard-001.json
└── packages/
    ├── pkg-002-invalidation/       # 增量：墓碑 + 代数回退 + 补齐缺失语义
    ├── pkg-003-full-replacement/   # 全量替换
    └── pkg-009-rollback/           # 版本回退，用于验证独立错误码
```

## CLI

全局参数可放在子命令前或后：

```bash
./workbench --db state/demo.sqlite3 status
./workbench status --db state/demo.sqlite3
```

支持命令：

| 命令 | 作用 |
|---|---|
| `load` | 解析、校验并原子写入输入基线；非法输入返回 `WB_*` 错误码且不建状态 |
| `merge` | 合并 v1/v2；可用 `--package` 应用版本化快照包 |
| `diagnose` | 计算水位/闭包并调度诊断任务，支持 `--budget-ms`、`--concurrency`、`--subscriber-id` |
| `patch` | 生成或幂等重放订阅者增量补丁 |
| `resume` | 提交 prepared 半提交检查点并幂等补放缺失阶段 |
| `status` | 稳定排序 JSON；`--format markdown` 输出可视化表和影响链 |
| `verify` | 临时库重跑全链，逐字段比较水位、游标、补丁和诊断结论 |

常用参数：

```text
--sample-dir samples
--db state/workbench.sqlite3
--injection-clock-ms 5000
--clock-ms 8000
--budget-ms 100000
--concurrency 8
```

## 增量失效演示

```bash
./workbench diagnose
./workbench merge --package samples/packages/pkg-002-invalidation
./workbench diagnose --clock-ms 9000
./workbench status --format markdown
```

该包使 `payments/0/0` 出现墓碑、`payments/1/1` 代数回退，并补齐 `inventory/1/1`。闭包精确覆盖 6 个分区，仅复用 `inventory/0`；未受影响分区的水位 JSON 哈希保持不变。

版本回退：

```bash
./workbench merge --package samples/packages/pkg-009-rollback
# exit code 3
# error.code = WB_SNAPSHOT_VERSION_ROLLBACK
```

## 中断恢复演示

```bash
./workbench load --db state/crash.sqlite3
WB_CRASH_POINT=checkpoint_after_prepare \
  ./workbench diagnose --db state/crash.sqlite3

./workbench status --db state/crash.sqlite3
./workbench resume --db state/crash.sqlite3
./workbench verify --db state/crash.sqlite3
```

`WB_CRASH_POINT` 还支持 `before_prepare`、`merge_after_compute_before_write` 和 `checkpoint_after_commit`，用于测试不同崩溃边界。

## 并发模型

多个进程可分别处理不同订阅者：

```bash
./workbench diagnose --subscriber-id sub-orders-a &
./workbench diagnose --subscriber-id sub-orders-b &
./workbench diagnose --subscriber-id sub-payments &
wait
```

文件锁串行化关键写事务，SQLite 幂等账本保证：

- 同一分区、同一语义绑定只提交一次；
- 同一订阅游标区间只推进一次；
- 重复运行返回相同补丁和诊断内容。

## 目录结构

```text
delivery_workbench/
├── canon.py       # 规范 JSON、哈希和字段 diff
├── cli.py         # argparse CLI 与 Markdown 可视化
├── engine.py      # 确定性编排、检查点计划和恢复
├── errors.py      # 业务错误码与退出码
├── loader.py      # 输入解析和全量校验
├── patches.py     # 变更序列、游标和补丁
├── scheduler.py   # 预算/并发调度与三类结局
├── semantics.py   # 两代快照裁决和版本包
├── storage.py     # SQLite schema、文件锁和幂等提交
├── verifier.py    # 全链重跑与逐字段验证
└── watermark.py   # 依赖闭包和水位定点计算

seed_samples.py
tests/test_workbench.py
scripts/run_acceptance.py
docs/TECHNICAL.md
docs/VERIFICATION.md
```

## 验证结论

当前本地确定性样本已通过 8 个 unittest 和一键验收：基线水位正确、增量闭包精确、SIGKILL 恢复后 `verify.field_diff_count=0`、四进程并发无重复计数、三种命令次序的稳定状态 JSON 相同。详见 `docs/VERIFICATION.md`。
