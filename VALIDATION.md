# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）。

运行环境：

- WSL Ubuntu 22.04 兼容环境
- Python `3.10.12`
- 标准库 `sqlite3` 链接 SQLite `3.37.2`
- 未安装/访问任何第三方依赖或网络服务

## 1. 样本生成与端到端验收

执行：

```bash
python3 generate_samples.py --out samples
bash scripts/run_acceptance.sh
```

实际结果：

- 生成 3 个租户、36 条条目、3 个 v2 分片和 v2 SQLite 元数据库。
- `python3 -m unittest discover -s tests -b`：`Ran 8 tests ... OK`。
- 规范 CLI 流程：`load -> merge -> admit -> rebalance -> evict -> status -> verify`。
- `verify` 返回 `ok=true`、`consistency_mode=exact_decision_sequence`、
  `verified_decisions=44`。
- 规范状态哈希：
  `6b3a4c5d3af536272cb8cc6c0c30da339029e7d7c0f0987736963f84f12acb01`。

最新 JSON 证据保存在：

- `artifacts/acceptance/merge.json`
- `artifacts/acceptance/admit.json`
- `artifacts/acceptance/rebalance.json`
- `artifacts/acceptance/evict.json`
- `artifacts/acceptance/status.json`
- `artifacts/acceptance/verify.json`

## 2. 业务结果

固定种子样本的实际裁决计数：

| 阶段 | 结果 |
| --- | --- |
| 准入 | 34 admitted，2 denied（墓碑），0 deferred |
| 重平衡 | 2 migrated，7 abandoned，0 failed（正常流程） |
| 重平衡预算 | 预算 10ms，已用 8ms，剩余 2ms |
| 逐出 | 5 evicted，原因均为 `soft_quota` |
| 账本字节 | 245760 bytes |
| 决策序号 | 1..44，无缺口 |

逐出键：

```text
cache-0005, cache-0016, cache-0019, cache-0033, cache-0034
```

重平衡迁移：

```text
cache-0009 L1 -> L2
cache-0010 L1 -> L2
```

使用 `--fail-key cache-0012` 的故障分类测试实际得到：

- `failed_after_start=1`
- 错误码 `E_INJECTED_START_FAILURE`
- 同轮仍有 `migrated=2`
- `abandoned=6`

这验证了“启动后失败”和“预算主动放弃”是不同结局。

## 3. 增量闭包

初始 v1-only 到完整 v1+v2 的差异覆盖较广，闭包为 36/36。

对局部补丁 `samples/policy_update_example.json`（仅修改 `cache-0001`），基于规范流程
结束时的当前层计算得到：

```json
{
  "changed_policy_keys": ["cache-0001"],
  "affected_tenants": ["alpha"],
  "affected_layers": ["L2", "L3"],
  "recompute_count": 27,
  "reused_count": 9
}
```

`test_incremental_closure_reuses_unaffected_entries` 分别全局运行旧策略和打补丁后的策略，
逐键比较最终 `(status, current_layer)`；闭包外 9 个条目全部一致，证明这些条目可安全复用。

## 4. 时钟回拨

在水位为 49000ms 的账本上执行：

```bash
python3 -m cache_ledger admit --db state/acceptance.sqlite3 \
  --samples samples --clock-ms 1000
```

实际退出码为 `10`，错误码为 `E_CLOCK_ROLLBACK`，容差 250ms，回拨差值 48000ms。
验证决策数仍为 44，且没有新增非 completed 命令日志。

## 5. 强制终止与恢复

验证了三类故障点：

1. `CACHE_LEDGER_CRASH_AFTER_PREPARE_KEY=admit:cache-0001`
   - 进程退出码 86；
   - 恢复前：1 个决策（policy merge）、1 个 prepared op、1 个 running command；
   - `resume` 重放 `admit:cache-0001` 并完成 admit 批次；
   - rebalance/evict 后 `verify` 通过，44 个决策，哈希与规范流程相同。

2. `CACHE_LEDGER_CRASH_BEFORE_COMMIT_KEY=admit:cache-0001`
   - 事务内 SQL 已执行但未 commit，进程退出码 86；
   - SQLite 回滚未提交账本变更，仅保留 prepared op；
   - `resume` 后验证通过，44 个决策，精确重放一致。

3. 真实 `SIGKILL`
   - 设置 `CACHE_LEDGER_HANG_AFTER_COMMITS=2` 和 marker 文件；
   - 子进程提交 merge + 2 个 admit 决策后挂起；
   - 外部执行 `kill -9 $(cat artifacts/kill9.marker)`；
   - `status` 显示 3 个决策、1 个 running command、0 个 prepared op；
   - `resume` 幂等续跑，最终 `verify` 返回 exact 模式和同一规范哈希。

## 6. 多进程并发

在 merge 后同时启动三个进程：

```bash
python3 -m cache_ledger admit --tenant alpha &
python3 -m cache_ledger admit --tenant beta  &
python3 -m cache_ledger admit --tenant gamma &
wait
```

实际结果：

- 总决策数 37：1 个 policy merge + 36 个条目裁决；
- 所有幂等键和决策序号唯一；
- 租户用量、层用量和字节预算均满足守恒；
- `verify` 使用 `semantic_idempotency_key_concurrent` 模式通过。

确定性全局分配计划保证三个进程得到相同的条目目标层；文件锁和 SQLite 写事务只负责串行化
提交和分配连续序号。连续 6 次全新并发运行的 `verify` 均返回 0。

## 7. 复现实验命令

```bash
python3 -m unittest discover -s tests -v

# 半提交恢复
DB=state/manual-recovery.sqlite3
python3 -m cache_ledger load  --db "$DB" --samples samples
python3 -m cache_ledger merge --db "$DB" --samples samples
CACHE_LEDGER_CRASH_AFTER_PREPARE_KEY=admit:cache-0001 \
  python3 -m cache_ledger admit --db "$DB" --samples samples || true
python3 -m cache_ledger resume --db "$DB" --samples samples
python3 -m cache_ledger rebalance --db "$DB" --samples samples
python3 -m cache_ledger evict --db "$DB" --samples samples
python3 -m cache_ledger verify --db "$DB" --samples samples
```
