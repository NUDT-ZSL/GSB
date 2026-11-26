# 实际验证记录

验证日期：2026-11-27

环境：

- Python 3.10.12
- NumPy 2.0.0（满足 `numpy>=1.26`）
- SQLite 3.37.2
- Linux/WSL 风格本地 shell；未使用 Docker、外部服务或网络

## 1. 一键验收

执行：

```bash
bash scripts/verify_acceptance.sh
```

实际结果：

```text
Ran 15 tests in 9.578s

OK
...
scripts/verify_acceptance.sh: ... Killed ... --hard-crash
...
recovery byte-comparison: OK
[5/5] Acceptance verification passed
```

脚本覆盖样本生成、15 个自动化测试、并行全流程、真实 SIGKILL、重启恢复和逐字节比对。

## 2. 确定性样本

执行：

```bash
python3 generate_samples.py --out samples
```

固定样本指标：

```text
sample_rate   = 16000
pcm_samples   = 224000
frame_samples = 320 (20ms)
hop_samples   = 160 (10ms)
frame_count   = 1399
```

本次配置下的确定性哈希：

```text
PCM SHA256       = 7c3f24d714cb97201d1c2d74be44eb895df4612e7a8ee6c3a318429562dfdcbd
truth SHA256      = ada95cb67edb4a8deb0f037d9cdc8c49efecec8a41f180e8eb0e858cda8735e8
v1 SHA256         = d117ab4c91cb6d81a61d6d24caf630518805cd44f99aa28d427fe93cfec6fac5
v2 manifest SHA256= d41afe4a4f146f116ceaaea22903481ff6749243942c592611b5c83c162c2ef4
```

连续生成到两个目录后，PCM、`frame_truth.jsonl`、v1 JSON 和 `manifest.json` 均逐字节相同。

## 3. 状态机与异常

干净全流程：

```bash
python3 run.py --mode all --output /tmp/emotion_final_all --workers 2
```

实际摘要：

```json
{
  "frame_count": 1399,
  "anomaly_count": 3,
  "cross_version_equivalent": true,
  "incremental": {
    "truth_recomputed_frames": 749,
    "generation_recomputed_entries": 639
  }
}
```

情感状态计数：

```text
angry=502, calm=398, happy=263, neutral=234, sad=2
```

异常检测结果与注入事件的帧区间完全一致：

| 类型 | 帧区间 | 置信度 |
| --- | ---: | ---: |
| `abnormal_pause` | `[600,690)` | 0.97 |
| `floor_grab` | `[910,940)` | 0.95 |
| `interruption` | `[1160,1190)` | 0.95 |

干净运行的阶段版本为：

```text
[feature_extraction v1, state_anomaly v2, ledger_migration v3, report_export v4]
```

## 4. 并行调度与预算

正式 `artifacts/diagnostic_report.json` 记录：

```text
used_process_pool = true
worker_count      = 2
batch_count       = 22
dispatch_order    = merge_order = 0..21
planned_transition_count = 1399
resident_feature_bytes   = 1991031
max_transitions          = 20000
max_feature_memory_bytes = 67108864
```

两个独立进程池输出中的 `emotion_states.jsonl`、`anomalies.json` 和 `unified_ledger.jsonl` SHA256 相同；进程池输出也与单进程顺序实现相同。

预算测试验证：

- 转移次数和内存同时超限：优先返回 `E_LOOP_BUDGET`，降级类别 `loop_budget_rejected`；
- 仅内存超限：返回 `E_RESOURCE_LIMIT`，降级类别 `resident_feature_memory_rejected`。

## 5. 账本错误码与迁移

自动化测试构造了损坏样本并验证：

- 未知版本 -> `E_VERSION_UNSUPPORTED`
- v1/v2 缺字段 -> `E_FORMAT_CORRUPT`
- 数值不可解析 -> `E_PARSE_ERROR`
- v2 绝对毫秒倒退 -> `E_FORMAT_CORRUPT`
- 记录/SQLite 校验和不一致 -> `E_CHECKSUM_MISMATCH`
- 严格降级非零修订号 -> `E_SEMANTIC_LOSS`

默认账本迁移结果：

```text
cross_version_equivalent   = true
roundtrip_equivalent       = true
against_original_equivalent= true
completion_count           = 5596
discard_count              = 0
metadata_restorable        = true
```

高代数演示账本从第 760 帧起带有 `generation=2/revision=1`，降级时输出非零 `revision` 的显式丢弃记录；帧区间、说话人和情感语义往返仍等价。

## 6. 增量失效

真值修正演示修正第 650 帧：

```text
state reused ranges     = [[0, 650]]
state recompute ranges  = [[650, 1399]]
reused_state_frames     = 650
recomputed_state_frames = 749
```

v2 高代数账本演示：

```text
ledger reused ranges     = [[0, 760]]
ledger recompute ranges  = [[760, 1399]]
reused_ledger_entries     = 760
recomputed_ledger_entries = 639
```

## 7. SIGKILL 与恢复一致性

验收脚本在 `state_anomaly` 阶段写入未提交 work 后发送 SIGKILL。重启时观察到：

```text
recovery.discarded_uncommitted = ["state/work/state_anomaly.work.json"]
feature_extraction.reused      = [[0,1399]]
state_anomaly.recomputed       = [[0,1399]]
snapshot versions              = [1,2,3,4]
```

恢复目录与无中断干净目录逐字节比较以下文件，全部一致：

```text
frame_features.jsonl
frame_feature_matrix.npy
emotion_states.jsonl
anomalies.json
unified_ledger.jsonl
migration_summary.json
```

## 8. 最新干净运行核心哈希

来自 `/tmp/emotion_final_all` 的一次实际运行：

```text
frame_features.jsonl       41799bf1b4c72f15575edeaff8bfa35d14800bc035610ec56e85b5d10c4b9572
frame_feature_matrix.npy   238ee61893a0d0adf1e0c30e33270d58cebf194d899dea2399e6e791539e936d
emotion_states.jsonl       fabf4a0e93f65c13933cb2d84bfa6b7b9e46b0d291cdc8dcc7fef25c303db208
anomalies.json             96cb2ae6e815d87337e0aebe2d3ef9ca9ed017174a75cedd61d3f7b1652e8103
unified_ledger.jsonl       62a5d73c508351fc01b54f179c57e0f4d9be48a6dcc06cd53ebabe6836077e86
```

重复执行或在另一输出目录执行时，上述核心产物哈希保持一致；SQLite 与快照中的版本/路径元数据按提交语义更新。
