# 实际验证记录

验证环境：

```text
Python 3.10.12
NumPy 1.26.4
工作目录 /home/zsl/feedback/uid2d1b8ac8-default-223
```

实现只使用 Python 标准库与 NumPy。Windows 兼容性依赖 `pathlib`、`os.replace`、
`argparse`、`subprocess` 和本地文件锁，提供的 `.bat` 脚本可直接运行演示。

## 1. 自动化测试

命令：

```bat
python -m unittest discover -s tests -t . -v
```

结果：

```text
Ran 31 tests in 18.598s

OK
```

覆盖项：

- 5 个必选业务错误码及 `job_id` / `field`
- GEMM/BMM 形状、批次、dtype、block_size、accum_mode 异常
- 策略记录逐字节确定性与 M/N/K 全覆盖
- 缓冲超限降级类别与分块数上限
- Kahan/Neumaier 补偿与参考容差
- 容差越界元素索引和偏差量
- 次正规数清理与定向舍入
- 部分阶段禁止提交
- 原子提交记录、旧版本回滚保护
- 校验和损坏、截断 JSON、CURRENT 校验和变化
- 并发提交立即 `E_STAGE_BUSY`
- 真实子进程 `os._exit(97)` 后恢复
- 恢复后逐元素结果与逐任务摘要一致
- 输出篡改触发 `E_INCONSISTENT`
- `CURRENT.json` 缺失时无重算修复
- 非演示目录不会被演示命令清理

## 2. 样本生成

命令：

```bat
python -m bmm_pipeline.cli generate --out samples\jobs.jsonl --seed 20261122
```

结果：

```json
{
  "job_count": 8,
  "jobset_fingerprint": "sha256:463f8dce7122d2ce8ab880c265509d5a30cca316ddb4a91fe6d0f7d45307de41",
  "seed": 20261122
}
```

样本同时包含 GEMM、BMM、batch=1 BMM、行主序、列主序、三种累加模式和三种 dtype。

## 3. 强制终止与恢复演示

命令：

```bat
python -m bmm_pipeline.cli demo --work-dir .demo --fault-after-blocks 20
```

结果：

```json
{
  "ok": true,
  "fault_after_blocks": 20,
  "crash_exit_code": 97,
  "committed_stages": ["validation", "planning"],
  "resumed_from_stage": "planning",
  "rebuilt_stages": ["accumulation", "aggregation"],
  "discarded_blocks": 20,
  "summary_byte_identical": true,
  "job_count": 8,
  "verification_checks": 73,
  "verification_ok": true
}
```

8 个任务的 `elementwise_identical` 全部为 `true`，包括 plain/Kahan/Neumaier、
GEMM/BMM、batch>1、batch=1、row/col 与 float16/32/64。

## 4. 状态面板

命令：

```bat
python -m bmm_pipeline.cli status --run-dir .demo\restarted
```

关键结果：

```json
{
  "status": "complete",
  "current_stage": "aggregation",
  "snapshot_version": 4,
  "job_count": 8,
  "completed_count_by_stage": {
    "validation": 8,
    "planning": 8,
    "accumulation": 8,
    "aggregation": 8
  },
  "compensation_interventions": 141,
  "flushed_subnormals": 0
}
```

偏差谱共 8 个任务，恢复运行最大绝对偏差为 `7.105427357601002e-15`。

## 5. 跨模块一致性自检

命令：

```bat
python -m bmm_pipeline.cli verify --run-dir .demo\restarted
```

结果：

```json
{
  "ok": true,
  "checks_run": 73,
  "issues": []
}
```

检查分布：

```text
snapshot_completion   4
strategy_coverage     8
strategy_determinism  8
output_file           8
independent_recompute 8
partial_merge         8
reference_tolerance   8
row_col_layout        8
transpose_semantics   8
gemm_bmm_batch1       4
summary_file          1
```

## 6. 异常注入结果

| 注入 | 返回码 |
|---|---|
| GEMM 内维不匹配 | `E_SHAPE_INVALID` |
| BMM A/B batch 不一致 | `E_BATCH_MISMATCH` |
| dtype=`int32` | `E_DTYPE_UNSUPPORTED` |
| block_size=`-1` / `0` | `E_BLOCK_SIZE_INVALID` |
| accum_mode=`unknown` | `E_ACCUM_MODE_UNKNOWN` |
| 容差设为 0 且分块路径出现舍入差 | `E_TOLERANCE_EXCEEDED`，含索引与偏差 |
| 快照内容被修改 | `E_SNAPSHOT_CORRUPT`，含 stage/version |
| 提交锁被占用 | `E_STAGE_BUSY`，立即返回 |
| 尝试以版本 1 覆盖版本 2 | `E_SNAPSHOT_ROLLBACK` |
| 任务数 / 分块数超限 | `E_RESOURCE_LIMIT` |
| 输出 `.npy` 被篡改 | `E_INCONSISTENT`，含 category/job_id |

以上结果均由 `tests/` 中的自动化用例实际执行。
