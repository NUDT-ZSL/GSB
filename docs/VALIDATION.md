# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

环境：

- WSL Ubuntu 22.04
- Python 3.10.12
- NumPy 1.26.4
- 未使用 Docker；仅使用 NumPy 与标准库

## 1. 一键验收

命令：

```bash
bash scripts/verify_acceptance.sh
```

实际结果：

```text
[1/7] generate deterministic samples
[2/7] unit and fault-injection tests
Ran 9 tests in 4.914s
OK
[3/7] targeted recomputation from baseline to effective config
targeted recompute: OK
[4/7] repeated warm execution is byte-identical
byte-identical warm report: OK
[5/7] interrupt after two committed batches and restart
interrupt checkpoint: OK
restart recovery: OK
[6/7] old snapshot rollback is rejected
rollback rejection: OK
[7/8] final report verification
all report checksums: OK
[8/8] replay report parameters into a new directory
replay stable hashes: OK
ACCEPTANCE_OK
```

完整终端输出保存在 `artifacts/validation-run.txt`，机器可读摘要保存在 `artifacts/acceptance-summary.json`。

## 2. 本地固定种子样本

`python3 generate_sample.py --out-dir data/generated` 生成：

- 9 个批次、3 个监控周期。
- 1296 条样本。
- 3 条配置变更事件。
- 3 个故意注入并被校验捕获的值域/缺失异常。

实际异常分布：

- `B001`：1 个 `feature_above_domain`（`age=199`）。
- `B004`：1 个 `category_outside_domain`（`region=moon_base`）。
- `B007`：1 个 `missing_feature`（`amount=null`）。

## 3. 统计一致性

两个全新工作区使用相同输入、配置和 `--now-ms 1764000000000` 冷启动后：

```text
ledger/batch_statistics_ledger.json SHA-256 相同
LEDGER_BYTE_IDENTICAL
fingerprints identical for 9 batches
```

参考窗口为 `C00`，包含 `B000`、`B001`、`B002`；全量窗口包含全部 9 批。报告中的 `window_statistics.reference_window` 与 `window_statistics.full_window` 显式分离。

## 4. 定向重算

先生成并运行无 `batch_overrides` 的基线配置，再在同一工作区应用生效配置和事件日志。实际集合：

```json
{
  "targeted_recompute_set": ["B002", "B003", "B004", "B006"],
  "targeted_reused_set": ["B000", "B001", "B005", "B007", "B008"],
  "targeted_coverage_full": true
}
```

双向差异检查均为空：

- `event_closure_without_recompute=[]`
- `recompute_without_event_closure=[]`

目标工作区快照从 v1 整体更新到 v2；v1/v2 版本号均为 2，v2 分片与 SQLite 校验通过。

## 5. 重复执行

在已预热工作区连续运行两次，第二次与第三次报告逐字节一致：

```text
cmp report.warm.json execution_report.json
byte-identical warm report: OK
```

热运行结果为 9 批全部复用，`computed_batch_ids=[]`，快照内容校验和不变且版本保持为 1。固定参数下验收摘要中的热报告校验和为：

```text
205323e05462b3d8267d7ebf5d5dc00f37c9bb5f198709879025b1474825a984
```

## 6. 中断恢复

`--interrupt-after-batch 2` 的实际检查点：

```json
{
  "status": "interrupted",
  "committed_batches": ["B000", "B001"],
  "committed_stage_index": 2,
  "signal": 15
}
```

中断时保留 `B002` 的 inflight、staging 和 pending pointer；重启后实际结果：

```json
{
  "recovery_recovered": true,
  "recovery_skipped": ["B000", "B001"],
  "recovery_computed_count": 7
}
```

单元测试还启动真实子进程，以 `--batch-delay-ms 500` 运行后发送 SIGTERM，断言退出码为 143、恢复状态为 `interrupted`、信号为 15。

## 7. 错误码与修复

`tests/test_pipeline.py` 直接构造故障统计，验证：

- 分位倒挂返回 `E_QUANTILE_ORDER`，并带 `segment_index=0`。
- 零分母返回 `E_ZERO_DENOM`，并带批次和分段索引。
- 缺失分位以 `E_QUANTILE_ORDER` + `reason=missing_quantile` 失败，不会按零处理。
- 空边界数据返回 `E_BOUNDARY_STATS`。
- 跨周期实体翻转超过阈值返回 `E_BOUNDARY_FLIP`，并带两条回放证据。
- 旧快照回退返回 `E_SNAPSHOT_ROLLBACK`。

生成样本故意包含高频边界波动，因此常规报告包含 `E_BOUNDARY_FLIP`。实际翻转与迟滞修复结果：

- `approve_boundary`：46 次翻转，阈值 8；修复后残余 1 次。
- `review_boundary`：32 次翻转，阈值 2；修复后残余 0 次。

## 8. 快照、校验和与并发

- `python3 -m risk_governance.cli verify --workdir out/acceptance/targeted` 验证报告校验和、缓存哈希链和 v2 分片校验和均有效。
- 单元测试启动读线程持续调用 `verify_current_v2()`，主线程连续发布 11 个新版本；未读到缺失/损坏分片，最终版本为 12（含初始版本）。
- `snapshot-rollback --to-version 0` 实际输出 `E_SNAPSHOT_ROLLBACK`，CLI 退出码为 3。
- `replay` 到新目录后，批次指纹、错误码、trace 校验和、v1/v2 内容校验和全部匹配，`match=true`。

## 9. 复现命令

```bash
python3 generate_sample.py --seed 416 --out-dir data/generated
bash scripts/verify_acceptance.sh
python3 -m unittest discover -s tests -v
```
