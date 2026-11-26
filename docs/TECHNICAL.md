# 技术说明

## 1. 总览

引擎仅依赖 Python 标准库、NumPy 和内置 SQLite，不使用 Docker、外部服务或网络。

| 业务能力 | 主要模块 | 关键产物 |
| --- | --- | --- |
| 固定种子合成与特征 | `emotion_diag/samplegen.py`, `emotion_diag/features.py` | `samples/dialogue_16k_mono.pcm`, `frame_features.jsonl`, `frame_feature_matrix.npy` |
| 情感状态机 | `emotion_diag/emotions.py`, `emotion_diag/scheduler.py` | `emotion_states.jsonl` |
| 话轮异常 | `emotion_diag/anomalies.py` | `anomalies.json` |
| v1/v2 读取与校验 | `emotion_diag/ledger.py` | SQLite + 分片 JSON |
| 双向迁移 | `emotion_diag/migrate.py` | `migration/`, `migration_summary.json` |
| 内容缓存与失效 | `emotion_diag/cache.py`, `emotion_diag/incremental.py` | `cache/`, `incremental_*_report.json` |
| 快照恢复 | `emotion_diag/snapshots.py`, `emotion_diag/pipeline.py` | `state/*.latest.json`, `state/snapshots/` |

## 2. 确定性音频与分帧

默认种子为 `46301314`，采样率固定 16kHz，声道为单声道，PCM 以小端有符号 16-bit 存储。

- 帧长：`20ms => 320 samples`
- 帧移：`10ms => 160 samples`
- 第 `i` 帧：`[i * 160, i * 160 + 320)`
- 默认时长 14s：`224000` samples，完整帧数 `1 + (224000 - 320) // 160 = 1399`

合成器为每个说话人使用固定 F0、共振峰、能量等级、颤音和音节包络；抢话与打断区间允许两个说话人的采样叠加。输出前做确定性峰值归一化并转换为 `<i2`。

特征包括：

- 短时能量 `sum(x^2)`、RMS 能量包络；
- 过零率及相邻帧梯度；
- 自相关 F0 候选，最多三个，按分数降序、lag 升序裁决；
- 能量变化量、归一化变化率；
- 精确样本/毫秒边界。

同一输入的 JSONL、NPY 和逐帧真值均逐字节可复现；NPY 由 `np.save` 写入内存字节后原子替换。

## 3. 情感状态机

状态集合为 `neutral/calm/happy/angry/sad`。每帧根据以下因素计算候选分数：

- 当前说话人编号及说话人偏置；
- RMS 能量区间；
- 能量变化率；
- ZCR 与 ZCR 梯度；
- F0、是否语音；
- 前一状态迟滞加分。

每个输出记录包含 `state`、`previous_state`、`transition`、`confidence`、`speaker_id` 和 `basis`。候选同分时使用稳定键：

```text
(-score, speaker_id ASC, frame_index ASC, state_code ASC)
```

跨批次证据排序同样按分数、说话人、帧索引裁决。

## 4. 并行调度与预算

`scheduler.py` 按 `batch_frames` 生成连续区间，分派顺序和归并顺序均为批次号升序。

状态机具有顺序依赖性，因此进程池 worker 会计算每个批次对全部 5 个可能入状态的转移表；主进程按批次顺序使用真实入状态选择对应变体。这样既有本地进程池并行，又保证与单进程顺序状态机完全一致。

准入顺序显式声明为：

1. `frame_count > max_transitions`：`E_LOOP_BUDGET`，降级类别 `loop_budget_rejected`；
2. 驻留特征内存超过 `max_feature_memory_bytes`：`E_RESOURCE_LIMIT`，降级类别 `resident_feature_memory_rejected`。

两项同时超限时优先返回 `E_LOOP_BUDGET`。

## 5. 话轮异常

检测器使用逐帧真值说话人/话轮标签确定候选，再用 RMS 能量连续性、ZCR 变化和可选的 `active_speaker_ids` 佐证。

- `abnormal_pause`：两侧均有语音且静音长度达到 `long_pause_frames`；
- `floor_grab`：A 到 B 无停顿直接切换，能量连续且存在叠加/能量尾落证据；
- `interruption`：A-B-A 模式，B 段较短，随后同一 A 说话人/话轮恢复。

事件使用半开帧区间 `[start_frame, end_frame)`，并记录置信度、真值话轮标签和证据摘要。无事件时返回：

```json
{"status": "no_anomaly", "empty": true, "marker": "ANOMALY_LIST_EMPTY", "events": []}
```

默认样本的检测结果精确为：

- `abnormal_pause [600,690)`
- `floor_grab [910,940)`
- `interruption [1160,1190)`

## 6. 两代账本与错误码

v1 是单文件 JSON，记录 `time_s`/`emotion_tier` 扁平行。v2 包含：

- `manifest.json`：版本、分片和 SQLite 文件名；
- `shards/shard-*.json`：逐记录绝对毫秒、说话人、代数、修订号、校验和；
- `metadata.sqlite3`：`meta` 与 `records` 表，保存绝对毫秒、代数、修订号和校验和。

读取器按 `format/version` 头分派；缺少头时按记录字段集分派。

| 条件 | 错误码 |
| --- | --- |
| 未知格式或主版本 | `E_VERSION_UNSUPPORTED` |
| 必填字段缺失、时间为负、绝对毫秒倒退、SQLite/分片结构损坏 | `E_FORMAT_CORRUPT` |
| JSON 或数值字段不可解析 | `E_PARSE_ERROR` |
| 记录/SQLite 校验和不一致 | `E_CHECKSUM_MISMATCH` |
| 严格降级遇到不可恢复修订号或语义签名不一致 | `E_SEMANTIC_LOSS` |
| 转移次数预算超限 | `E_LOOP_BUDGET` |
| 驻留特征内存超限 | `E_RESOURCE_LIMIT` |

## 7. 双向迁移

统一内部表示以 `[frame_start, frame_end)` 帧区间、`speaker_id` 和 `emotion_tier` 为语义签名。

v1 升级 v2：

- `abs_ms = round(time_s * 1000)`，来源 `derived:round(time_s*1000)`；
- 缺失 `speaker_id` 从 `frame_truth.speaker_id` 补全；
- 缺失 `generation` 从 `config.default_generation` 补全；
- `revision` 默认 0；
- 每个字段补全都写入 `completion_sources`。

v2 降级 v1：

- 输出 `time_s = abs_ms / 1000` 与 `emotion_tier`；
- v1 无法表达非零 `revision`，按 `drop_nonzero_incremental_revision` 输出逐项丢弃记录；
- 普通降级保留可等价还原的帧区间/说话人/情感语义；
- `strict=True` 时，非零修订号无法原样恢复，返回 `E_SEMANTIC_LOSS`。

## 8. 内容缓存与增量传播

`ContentCache` 将特征、状态、异常事件和账本条目写入 `cache/objects/<hh>/<sha256>.json`。缓存键输入包含：

- `category`；
- PCM SHA256；
- 帧参数 SHA256；
- 标注代数；
- 上游产物哈希列表；
- 帧索引。

真值修正时，`diff_truth` 找到变更帧；由于状态机顺序依赖，从最早变更帧开始的后缀必须重算，前缀可复用。默认演示修正第 650 帧，因此：

- 复用状态帧：`[0,650)`；
- 重算状态帧：`[650,1399)`，共 749 帧。

v2 高代数账本从第 760 帧开始带来代/修订变化，因此：

- 复用账本条目：`[0,760)`；
- 重算/聚合后缀：`[760,1399)`，共 639 条。

## 9. 四阶段快照与恢复

阶段固定为：

1. `feature_extraction`
2. `state_anomaly`
3. `ledger_migration`
4. `report_export`

每个快照包含全局单调 `version`、阶段内 `stage_version`、`completed_ranges`、`content_checksum`、`snapshot_checksum` 和工件 SHA256。快照先写同目录临时文件，fsync 后 `os.replace` 原子替换。

处理阶段时写入 `state/work/*.work.json`。重启先删除未提交 work 和残留临时文件，再读取最近已提交快照：

- 特征阶段已提交：直接加载，不重算特征；
- 状态阶段未提交：丢弃 work，从完整特征前缀重建全部状态；
- 账本/报告阶段同理从最近提交点继续。

验收脚本会在状态阶段处理后发送真实 `SIGKILL`，确认留下未提交 work；重启后删除该文件并恢复，核心 JSONL/NPY/JSON 与无中断运行逐字节相同，快照版本仍为 `[1,2,3,4]`。

## 10. 常用命令

```bash
python3 generate_samples.py --out samples
python3 run.py --mode all --samples samples --output artifacts --workers 2
python3 -m unittest tests.test_engine -v
bash scripts/verify_acceptance.sh
```

编程入口可使用 `diagnostic_engine.py` 或直接导入 `emotion_diag`。
