# 实际验证记录

验证日期：2026-11-27  
环境：WSL Ubuntu 22.04，Python 3.10.12，NumPy 2.0.0  
样本种子：`20261127`

## 复现命令

```bash
python3 scripts/generate_data.py --out data
python3 -m pytest
python3 -m telemetry_drift verify --data data --out artifacts/verification
```

## 单元测试

```text
26 passed
```

覆盖：对齐字节确定性、两种缺失策略、窗口分数独立复算、上游贡献、闭包样本
限制、同分闭包合并、v1/v2 读取与迁移、未知版本错误、缓存键与篡改检测、
依赖传播、检查点保存点/半写文件/跨进程提交锁、账本重放、端到端代际一致性。

## 端到端验收

`artifacts/verification/verification.json` 中 `ok=true`，12 项检查全部通过：

- `snapshot_generations_consistent=true`
- `ledger_replay_ok=true`
- `cache_run_reused=true`
- `cache_targeted_invalidation=true`
- `upgrade_metadata_completed=true`
- `downgrade_records_drops=true`
- `unknown_version_error=true`
- `exit_report_resume=true`
- `atomic_checkpoint_ignores_half_temp=true`
- `exclusive_commit_busy_error=true`
- `threshold_precision_consistent=true`
- `subnormal_protected=true`

关键结果：

```text
window_count=5
drift_event_count=3
closure_count=1
closure covered=w0001,w0002,w0003,w0004
reused window=w0000
v1 comparison_digest=a521d397c2ca520ed88253ca1e637d1f100504c28b21f7055e939a2a9c4af042
v2 comparison_digest=a521d397c2ca520ed88253ca1e637d1f100504c28b21f7055e939a2a9c4af042
```

缓存验证：

```text
第一次运行：重算 w0000-w0004
相同输入第二次运行：run_hit=true，全部复用
变量 1 修订 [1800, 2200] ms：重算 w0001-w0004，复用 w0000
```

快照迁移验证：

```text
v1 -> v2：当前模型系数/截距不变，补全 created_ms，
          completion source=v1_relative_seconds_plus_epoch
v2 -> v1：显式丢弃 retrain_generation=1，
          reason=E_INCREMENTAL_RETRAIN_DROPPED
未知版本：E_VERSION_UNSUPPORTED
并发提交冲突：E_STAGE_BUSY
```

`telemetry_drift run` 的真实 CLI 烟测同样通过：输出
`artifacts/run-v1/`，阶段检查点最终为 `done`（版本 7），
`replay-ledger` 返回 `ok=true`。

