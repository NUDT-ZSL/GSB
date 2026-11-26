# 实际验证记录

验证日期：2026-11-27（Asia/Shanghai）

## 环境

- Python：3.10.12
- Python `sqlite3` 模块：3.37.2
- NumPy：2.0.0
- 操作系统上下文：WSL Ubuntu-22.04 兼容环境
- `sqlite3` shell：当前容器未安装；无免密 sudo，因此使用 Python 标准库 `sqlite3` 完成数据库验证。WSL 中可执行 `sudo apt-get install -y sqlite3` 补齐 shell CLI。

## 已执行命令

```bash
python3 -m compileall -q capacity_engine scripts tests
python3 -m unittest discover -s tests -v
python3 scripts/acceptance.py
python3 -m capacity_engine.cli verify --out <acceptance-workdir>/clean-a
```

## 结果摘要

### 单元测试

`artifacts/unittest-result.txt` 记录了完整输出：

- 7 个测试全部通过。
- 覆盖固定种子确定性、6 类非法输入错误码与“不写状态”、窗口不重不漏、双跑逐字节一致、精度预算、阈值来源优先级、失效范围、补丁幂等和崩溃恢复。

结果：

```text
Ran 7 tests in 12.714s
OK
```

`pytest 8.2.0` 下同样验证：`7 passed in 13.66s`。

### 端到端验收

结构化结果保存在 `artifacts/acceptance-result.json`，控制台日志保存在 `artifacts/acceptance-run.log`。最近一次运行：

```text
ok: true
report_count: 5
deterministic_two_output_directories: true
idempotent_rerun_same_directory: true
verification_ok: true
verification_check_count: 22
crash_reports_match_clean_run: true
```

样本规模：

- 3 个服务、3 个唯一实例。
- 360 条请求轨迹。
- 6 个全局窗口 × 3 个实例 = 18 个实例窗口摘要。
- 18 个实例窗口 × 4 个指标 = 72 条指标/对照记录。
- 1 个基线修订 + 4 个补丁修订 = 5 个修订。

### 崩溃恢复

验收脚本分别在以下阶段首次运行时注入 `os._exit(86)`，随后不带 `--crash-at` 恢复：

| 注入阶段 | 首次退出码 | 恢复 | 恢复后报告与干净运行一致 |
| --- | ---: | --- | --- |
| `window_aggregation` | 86 | 成功 | 是 |
| `fixedpoint_conversion` | 86 | 成功 | 是 |
| `threshold_merge` | 86 | 成功 | 是 |
| `patch_commit` | 86 | 成功 | 是 |

恢复后没有遗留 `running` 工作单元，`verify` 的 22 项检查全部通过。

### 多进程并发

4 个 CLI 进程并发对同一个 `--out` 执行 `run`：

```text
all_exit_zero: true
verify_ok: true
revision_count: 5
duplicate_ledger_sequence_groups: 0
```

文件锁使进程串行进入关键区；后完成的进程读取已提交修订并执行幂等重放。

### 失效闭包

固定样本中的精确失效规模：

- `patch-001-gold-cpu-threshold`：重算 6 条 gold CPU 结论和 27 个相关敏感对；容量值/余量账本全部复用。
- `patch-002-gold-shared-dependency`：依赖闭包影响 gold 实例的 24 条指标结论和 108 个敏感对；容量值/账本复用。
- `patch-003-silver-memory-tombstone`：重算 6 条 silver memory 结论和 27 个相关敏感对。
- `patch-004-cpu-scale-ten-thousand`：18 条 CPU 容量值和对应账本重算，63 个 CPU 敏感对重算，其余 54 条指标值/账本复用。

所有复用对象的摘要比较均为 `unchanged=true`。同一输出目录重复运行后，`reports/*.json` 字节不变。

### 精度与拐点

基线 72 条定点/Decimal 对照：

```text
budget_violation_count: 0
max_absolute_deviation: 0.05
max_relative_deviation: 约 5.1361e-4
sensitive_count: 27
higher_scale_flip_count: 3
fixed_decimal_conclusion_flip_count: 0
```

最大绝对偏差 `0.05` 来自标度为 10 的 P95 延迟，正好等于 half-up 单操作误差上界 `0.5 / 10 = 0.05`。

设计出的精度敏感示例：

```text
obs_a: W2:inst-checkout-1
obs_b: W3:inst-checkout-1
metric: latency_p95
difference: 0.002 ms
fixed_order at scale 10: equal
higher_scale_order at scale 1000: b_gt_a
ledger_seqs: [43, 44, 79, 80]
```

这表示粗标度把两个近邻 P95 都换算为同一整数，无法选择更接近容量拐点的窗口；更高标度和 Decimal 真值均可确定顺序。样本中没有固定通路与 Decimal 通路的最终阈值结论翻转，说明阈值距离未落入会导致错误判定的边界；预算越界错误码 `CAP-2001-PRECISION-BUDGET-EXCEEDED` 已由单元测试通过人工放大偏差验证。

## 非法输入验证

单元测试通过复制样本并变异字段，确认以下错误码且 CLI 不创建输出目录：

- `CAP-1001-VALIDATION-SCENARIO`
- `CAP-1002-VALIDATION-TRACE`
- `CAP-1003-VALIDATION-METRIC-DEFINITION`
- `CAP-1004-VALIDATION-THRESHOLD-V1`
- `CAP-1005-VALIDATION-V2-SHARD-HASH`
- `CAP-1006-VALIDATION-V2-GENERATION-MONOTONIC`

## 复现实验

```bash
python3 -m capacity_engine.cli seed --dir /tmp/capacity-sample
python3 scripts/acceptance.py --workdir /tmp/capacity-acceptance
python3 -m unittest discover -s tests -v
```
