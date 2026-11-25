# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

## 环境

- Python `3.10.12`
- NumPy `1.26.4`
- SQLite `3.37.2`
- 工作区所在磁盘可用空间约 `905G`，满足至少 `500MB` 要求
- 未使用 Docker 或其他容器；未访问外部服务

环境检查：

```bash
$ python3 scripts/check_env.py
{'python': '3.10.12', 'numpy': '1.26.4', 'sqlite': '3.37.2'}
```

## 一键验收

执行：

```bash
$ bash scripts/verify_acceptance.sh
...
Ran 10 tests in 1.323s
OK
...
== All acceptance checks passed ==
ACCEPTANCE_RC=0
```

完整机器可读输出位于：

- `out/acceptance-run.log`
- `out/final-acceptance.log`
- `out/validation.json`
- `out/acceptance-verify-baseline.json`
- `out/acceptance-threshold-replacement.json`
- `out/acceptance-eval-comparison.json`
- `out/acceptance-db-baseline.json`
- `out/acceptance-crash-db.json`

## 样本与精度结果

- 服务：3 个
- 实例：6 个
- 有效轨迹：1500 条
- 每个作用域窗口数：6
- 作用域总数：9（3 个服务 + 6 个实例）
- 提交窗口行：54
- 指标值：216（54 × 4 个指标）
- Decimal 全量核对：216 项全部在误差上界内
- `E_PRECISION_BUDGET`：正常样本 0 次
- 精度敏感配对：37 个
- 高标度会改变朴素排名/结论的配对：17 个

刻意构造的 `alpha-1` / `alpha-2` CPU 配对结果：

```text
absolute_difference = 0.00001
current scale       = 1000
current fixed       = 409 vs 409（朴素排名 tie）
probe scale         = 10,000,000
high-scale ranking  = alpha-1_preferred
safe_ranking        = precision_sensitive_indeterminate_within_tolerance
higher_scale_flip   = true
```

这表示粗标度朴素结论会从平局变为某一实例更优，但引擎的安全结论不依赖该舍入排名，因此受保护判定不会被舍入翻转。

## 阈值失效重算

gen3 变更：

```text
key: svc-alpha:cpu_utilization:warn
old: 0.65
new: 0.60
effective_logical_ms: 35000
```

失效闭包结果：

```text
recompute_count             = 9
reuse_evaluation_count      = 207
actual recomputed           = 9
actual reused               = 207
```

9 条重算对应 `svc-alpha`、`alpha-1`、`alpha-2` 三个作用域的第 3、4、5 窗口。窗口摘要、定点指标值、余量账本和误差链全部复用。

独立全新 gen3 全量库比对：

```json
{
  "identical": true,
  "incremental_rows": 216,
  "truth_rows": 216,
  "ok": true
}
```

## 并行与幂等

三个服务并行运行后：

```json
{
  "windows": 54,
  "metric_values": 216,
  "duplicate_windows": 0,
  "duplicate_metric_values": 0,
  "duplicate_ledgers": 0,
  "budget_violations": 0
}
```

两个相同 `--service svc-beta` worker 并发竞争时，服务文件锁串行化执行；最终仍为：

```json
{
  "windows": 18,
  "metric_values": 72,
  "duplicate_windows": 0,
  "duplicate_metric_values": 0
}
```

两个进程并发提交同一个 gen3 阈值快照时，全局阈值锁串行化；结果为一条已应用快照、一条 committed replacement，另一个调用返回 `idempotent_already_current`。

重复运行已完成服务时输出 `committed=0`、`skipped=18`。

## SIGKILL 恢复

窗口阶段点注入：

```text
python -m capdiag run ... --service svc-alpha \
  --crash-at stage --crash-window 2
CRASH_RC=137
```

恢复识别到 1 个 staged window prep；重放结果为 `committed=16, skipped=2`。最终：

```json
{
  "windows": 18,
  "metric_values": 72,
  "duplicate_windows": 0,
  "duplicate_ledgers": 0,
  "duplicate_ops": 0,
  "staged_preps": 0
}
```

阈值替换阶段点同样得到 `137`，恢复识别 1 个 staged threshold replacement；重新执行后 gen3 成为当前版本，旧 gen2 重放不会覆盖当前版本。

## 版本回退与非法输入

当前为 gen3 时提交 gen2：

```text
exit=2
error_code=E_VERSION_ROLLBACK
refusing to apply older threshold generation
```

非法轨迹样本校验：

```text
exit=2
E_TRACE_SEQUENCE: request_seq must be strictly increasing within instance
E_TRACE_CLOCK: logical_clock_ms must be a non-negative integer
E_SCHEMA_VALIDATION: trace service/instance is not declared in scenario
valid_trace_records=1
```

坏标度单元测试确认返回 `E_METRIC_SCALE`；篡改 v2 分片哈希的单元测试确认返回 `E_V2_SHARD_HASH`。

## 精度预算故障注入

测试中将一条 CPU 定点值故意改为 `999999` 后，`capdiag verify` 返回退出码 `3`，JSON 报告包含：

```text
error_code=E_PRECISION_BUDGET
register_ids=[...]
```

这验证了越界检测、登记序号保留和 CLI 失败语义。
