# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）

环境：WSL Ubuntu-22.04，Python 3.10.12，NumPy 1.26.4。

## 执行命令

```bash
python gen_samples.py --seed 0 --case_range 0 50
python -m unittest discover -s tests -v
python -m imgdiag.cli baseline --out artifacts/baseline_report.json
python -m imgdiag.cli parallel --workers 4 --batch-size 10 \
  --out artifacts/parallel_report.json
python -m imgdiag.cli report --out artifacts/diagnostic_report.json
python scripts/acceptance.py
```

## 结果摘要

| 验证项 | 实际结果 |
| --- | --- |
| 单元测试 | 12/12 通过 |
| 确定性任务 | 50 个全部生成并诊断 |
| float64 分块/整幅一致 | 50/50 `byte_identical=true` |
| 分块偏差谱 | 18474 个展平补全通道值全部落在 `exact_zero`，非零区间计数为 0 |
| 进程池调度 | 50/50 `all_ok=true`，两次调度计划 `scheduling_reproducible=true` |
| 强制终止 | 外部进程组收到 `SIGKILL`，退出码 -9；最近检查点版本为 4 |
| 恢复范围 | `edit-0000` 重启后复用 S1–S4，仅重算 `S5_metrics` |
| 幂等重跑 | 50 个任务均复用全部五阶段，输出哈希映射不变 |
| 检查点链 | `chain_valid=true`；恢复后全量自检时为 260 个检查点，幂等重跑后继续单调追加 |
| 独立查询重放 | `query edit-0007` 的 `replay_match=true` |
| 验收脚本总耗时 | 约 15.4 秒 |

机器可读结果：

- `artifacts/acceptance_report.json`
- `artifacts/diagnostic_report.json`
- `artifacts/baseline_report.json`
- `artifacts/parallel_report.json`
- `artifacts/unittest_final.log`

## 业务码探针

综合报告中的确定性非法样本全部得到预期码：

- `E_MASK_DEGENERATE`
- `E_MASK_INTERSECT`
- `E_MASK_OUT_OF_BOUNDS`
- `E_STYLE_INVALID`
- `E_TRANSFORM_INVALID`
- `E_MEMORY_BUDGET`
- `E_BLOCK_CONCURRENCY`

另外，旧缓存键对修订任务可复现：

```bash
python -m imgdiag.cli stale edit-0003 \
  --workdir runtime/acceptance-kill-<latest> \
  --revised-samples samples/edit_tasks_rev2_style.jsonl
```

返回 `E_STALE_RESULT`，`stale_fields` 至少包含 `revision` 和 `style_strength`。

## 预算与降级

`artifacts/diagnostic_report.json` 的 `budget_adjudication` 包含：

- `tight_conflict`：紧总内存预算下的 worker 收缩和 `SMALLER_BLOCK` 任务集合。
- `serial_degradation`：重任务在 520 KiB 测试预算下得到 `SERIAL`，驻留估算约 436064 字节。
- `impossible_memory`：4096×4096 探针返回 `E_MEMORY_BUDGET`。
- `strict_concurrency`：99 并发请求在严格策略下返回 `E_BLOCK_CONCURRENCY`。
- `policy_tradeoff`：质量优先保持 `strict`；进度优先且显式允许时采用 `standard` 中间检查强度，但最终 float64 一致性闸门不放宽。

## 精度诊断

50 个样本的平均 RMSE 趋势：

- `float64`：所有级别为 0，主导误差级别为 `null`。
- `float32`：约 `1e-7` 到 `1e-5` 量级，主导级别为 `style_shading`。
- `fixed`（任务显式给出 64/128/256 `quant_scale` 与舍入模式）：约 `1e-1` 量级，主导级别分布在 `boundary_sampling` 与 `style_shading`。

每个任务的 S5 缓存均保存各级 actual/reference NPZ 明细；`selfcheck.query_edit()` 会重新调用 `recompute_stage_diagnostics()` 并比较主导级别和每级 RMSE。

## 修订失效验证

自动化测试和 CLI 均验证：

- 仅 `style_strength` 变化：复用 `S1_mask_boundary`、`S2_completion`，重算 `S3_style`、`S4_fusion_blocks`、`S5_metrics` 及块下游。
- `mask` 变化：修订计划将该任务五个阶段全部标记为重算。
- 未受影响任务：五阶段全部复用。
- `recompute_set` 与 `reuse_set` 不相交，且并集覆盖全部任务。

可复现命令：

```bash
mkdir -p runtime
W=$(mktemp -d runtime/rev-XXXXXX)
python -m imgdiag.cli run --workdir "$W" --batch-size 10
python -m imgdiag.cli revise --workdir "$W" \
  --old samples/edit_tasks.jsonl \
  --new samples/edit_tasks_rev2_style.jsonl
```
