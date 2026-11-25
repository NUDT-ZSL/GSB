# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

环境：WSL Ubuntu 22.04，Python 3.10.12；仅使用标准库运行时，测试使用 pytest。

复现命令：

```bash
bash scripts/acceptance.sh
```

完整控制台输出保存在 `artifacts/acceptance-run.txt`，机器可读摘要保存在 `artifacts/verification-summary.json`。

## 结果摘要

- 自动化测试：`12 passed`。
- 固定种子：`20261126`。
- 合并步骤：8 个；初始 `judge`：8 个可复用，其中 1 个进入分桶采样。
- 采样步骤：`bench-cache`；候选数 17，阈值 12；16 桶抽 10 桶，先验漏判上界 `0.375`。
- 一次性构建：8 个层对象，`verify` 的 `mismatch_count=0`，SQLite `integrity_check=ok`。
- 强杀点 `object_renamed`：退出码 88；`resume` 识别并补提交 `final-image`；恢复后与 `.acceptance/one` 的 8 个对象逐字节一致。
- 另外两个测试覆盖 `temp_object` 与 `cache_commit` 强杀点，恢复后同样逐字节一致。
- 预算场景：并发 2、轮次预算 4 时，`pkg-config` 准入并完成，`locale-data` 因 `aggregate_logical_budget_exhausted` 记为 `abandoned_budget`，CLI 退出码 30；放大预算后 `resume` 成功且与一次性运行逐字节一致。
- 并发上限场景：并发 1 时，第二轮就绪步骤产生 `not_started_within_budget / concurrency_limit_for_round` 事件。
- 参数变更：`--set-arg BUILD_CHANNEL=canary` 的失效闭包为 `app-layer -> final-image -> bench-cache`；5 个上游/旁支步骤保持复用，未受影响对象字节相同。
- 多进程：两个相同 `pkg-config` worker 与一个 `locale-data` worker 并发后，`pkg-config` 物化记录和缓存行均只有 1 条。
- 时钟回拨：对水位 `1764028824017` 注入 `--clock-ms 1` 返回 `E_CLOCK_ROLLBACK`，退出码 25，诊断含 `backward_ms=1764028824016`、`tolerance_ms=2`。

## 摘要清单哈希

一次性运行与崩溃恢复后的 `reports/digests.json` SHA-256 相同：

```text
b871b2637fd2db1e233fb8254fbe3cc863ee4749d7cc1a434ce8a2e98c512273
```

`verify --reference-dir .acceptance/one` 的关键结果：

```json
{
  "ok": true,
  "mismatch_count": 0,
  "reference_check": {
    "digests_byte_identical": true,
    "object_file_count": 8
  },
  "sqlite_integrity": "ok"
}
```

## 错误码覆盖

测试 `test_validation_error_codes_and_diagnostics` 与 `test_clock_and_sampling_conflict_error_codes` 覆盖：

- `E_PATH_ESCAPE`
- `E_BASE_LAYER_MISSING`
- `E_GENERATION_ROLLBACK`
- `E_SHARD_HASH_MISMATCH`
- `E_LAYER_CONFLICT`
- `E_SAMPLE_DIGEST_CONFLICT`
- `E_CLOCK_ROLLBACK`

其中路径越界还通过 CLI 验证退出码 20；时钟回拨通过 CLI 验证退出码 25。
