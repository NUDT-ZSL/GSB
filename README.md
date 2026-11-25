# 多租户分级缓存账本裁决引擎

纯 Python 3.10+ 标准库实现的确定性缓存准入、层级迁移、逐出、容量重平衡与崩溃恢复系统。
样本由固定种子 `436202611` 本地生成；不依赖网络、Docker 或第三方 Python 包。

## 快速开始

```bash
python3 --version
python3 generate_samples.py --out samples

DB=state/cache.sqlite3
python3 -m cache_ledger load      --db "$DB" --samples samples
python3 -m cache_ledger merge     --db "$DB" --samples samples
python3 -m cache_ledger admit     --db "$DB" --samples samples
python3 -m cache_ledger rebalance --db "$DB" --samples samples
python3 -m cache_ledger evict     --db "$DB" --samples samples
python3 -m cache_ledger status    --db "$DB" --samples samples
python3 -m cache_ledger verify    --db "$DB" --samples samples
```

也可直接运行端到端验收脚本：

```bash
bash scripts/run_acceptance.sh
python3 -m unittest discover -s tests -b
```

根目录还提供 `main.py` 转发到同一 CLI：`python3 main.py status --db state/cache.sqlite3`。

## 子命令

- `load`：严格校验租户、JSONL 条目、容量、v1 JSON、v2 分片和 SQLite 元数据库，并初始化账本。
- `merge`：合并 v1/v2 策略，换算 v1 相对秒数，记录定点余量、裁决依据和增量失效闭包。
- `admit`：按固定全局分配计划裁决准入和目标层级；支持 `--tenant` 分片并行。
- `rebalance`：在逻辑时间预算内按冷却窗口、阈值、目标层容量和逐出优先级权重调度迁移。
- `evict`：处理墓碑、租户软配额和总字节压力。
- `resume`：提交 prepared 半提交操作，并重放中断的命令批次。
- `status`：输出键排序稳定的租户用量、层级占用、策略指针、决策数和恢复状态。
- `verify`：检查守恒/上限/非负约束，并把命令日志在全新 SQLite 中全局重放比对。

常用参数：

- `--clock-ms N`：注入逻辑时钟；缺省使用样本中的确定性时钟安排。
- `--clock-tolerance-ms N`：时钟回拨容差。
- `--tenant NAME`：只处理指定租户。
- `--time-budget-ms N`：本轮重平衡逻辑时间预算。
- `--fail-key CACHE_KEY`：令指定候选在启动后失败，用于验证失败分类。
- `--report PATH`：写入完整 JSON 结果。

## 确定性样本

`generate_samples.py` 生成：

- `samples/tenants.json`：3 个租户、块配额比例和逐出优先级权重。
- `samples/entries.jsonl`：36 条严格递增条目，摘要绑定序号、键和块大小。
- `samples/capacity.json`：L1/L2/L3 块上限、字节预算、阈值、冷却和预算。
- `samples/policy_v1.json`：相对生效秒数与扁平键到层映射。
- `samples/policy_v2/shards/*.json`：3 个哈希校验分片。
- `samples/policy_v2/policy_v2_meta.sqlite`：分片哈希、绝对毫秒、代数、墓碑和失效标记。
- `samples/policy_update_example.json`：用于演示局部增量闭包的策略补丁。

## 验收快照

固定样本的规范流程产生 44 个连续决策：

- 准入：34 条 admitted，2 条墓碑 denied，0 条 deferred。
- 重平衡：2 条迁移，7 条预算主动放弃；预算 10ms，使用 8ms。
- 逐出：5 条软配额逐出。
- 守恒：245760 bytes，决策序号 1..44 无缺口。
- 全局重放：`exact_decision_sequence`，规范状态哈希
  `6b3a4c5d3af536272cb8cc6c0c30da339029e7d7c0f0987736963f84f12acb01`。

实际测试和崩溃/并发演练记录见 `VALIDATION.md`；设计与需求追踪见 `docs/TECHNICAL.md`。
