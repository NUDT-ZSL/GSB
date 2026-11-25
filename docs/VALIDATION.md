# 实际验证结果

验证时间：2026-11-26（Asia/Shanghai）。

运行环境：

- Python 3.10.12（项目声明兼容 Python 3.9+）
- NumPy 1.26.4
- Linux/WSL 工作目录

## 1. 确定性样本与单元测试

执行：

```bash
python scripts/generate_deterministic_fixtures.py
python -m unittest discover -s tests -v
```

实际结果：

```text
window_count: 96
seed: 406
Ran 14 tests in 2.746s

OK
```

测试覆盖：

- 重构窗口与隐层激活逐字节一致；
- 反向传播梯度与数值梯度一致；
- 掩码边界可追溯到 `window_id`、展开维度和传感器坐标；
- v1 -> v2 -> v1 分位迁移和裁决一致；
- v2 专有层级在降级时按规则丢弃并记录；
- 伪标签修正只重算受影响分段，其他分段校验和不变；
- 预算估算与降级样本选择；
- 并发校准冲突返回 `E_STAGE_BUSY`；
- 漂移归因、时间窗和模块集合；
- 严格/容差带裁决；
- 快照版本与 `E_SNAPSHOT_ROLLBACK`；
- 批次追加；
- 真实子进程 `SIGKILL` 后恢复并与无崩溃基线比对。

## 2. 一键验收

执行：

```bash
python scripts/run_acceptance.py
```

实际报告（`artifacts/acceptance/acceptance_report.json`）：

```json
{
  "ok": true,
  "checks": {
    "unit_tests_pass": true,
    "sigkill_observed": true,
    "resume_pass": true,
    "committed_batches_reused": true,
    "remaining_batches_processed": true,
    "decisions_match_baseline": true,
    "record_checksums_match_baseline": true,
    "replay_pass": true,
    "fixture_seed": 406,
    "window_count": 96
  },
  "unittest_returncode": 0,
  "crash_returncode": -9,
  "resume_returncode": 0,
  "replay_record_count": 160,
  "baseline_record_count": 160,
  "recovered_record_count": 160,
  "snapshot_versions": {
    "plan": 13,
    "result": 14,
    "threshold": 3
  }
}
```

`crash_returncode = -9` 表示示例子进程在第 3 个批次快照落盘后通过 `SIGKILL` 被强制终止。恢复运行实际复用前 3 个批次共 24 个窗口，仅处理剩余 72 个窗口；恢复后的 `decisions` 与全部 160 条记录校验和序列均和无崩溃基线完全一致。

## 3. 可回放性

执行：

```bash
python -m drift_ae replay artifacts/acceptance/recovery/diagnostics.jsonl
```

实际摘要：

```json
{"ok": true, "record_count": 160, "mismatch_count": 0}
```

每条记录包含：

- 窗口、分段、批次和标签信息；
- 掩码、补齐值、重构值和配置；
- AE 权重校验和、隐层/重构指纹；
- 四类误差特征及校验和；
- 门限策略、阈值行、阈值模型版本与校验和；
- 校准/重校准路径和传播清单。

## 4. 其他手动验证

- `--strategy tolerance_band` 端到端运行：160 条记录全部重放成功。
- `--threshold-view v1` 端到端运行：160 条记录全部重放成功；v1/v2 等价阈值行的裁决一致性由迁移测试验证。
- 快照回退 API 实际返回 `E_SNAPSHOT_ROLLBACK`。
- 两个独立 `CalibrationGate` 对象竞争同一状态目录时，后来者立即得到 `E_STAGE_BUSY`，释放后可获取锁。
