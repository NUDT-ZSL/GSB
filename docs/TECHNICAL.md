# 技术说明

## 1. 确定性包络

帧起点完全由样本索引计算：

```text
start[i] = i * frame_hop
保留条件：start[i] + frame_length <= sample_count
```

默认 `frame_length=320`、`frame_hop=160`，因此 192000 samples 的样本产生 1199 个完整块。每个块独立计算：

- 短时能量 `sum(x^2)`
- RMS、dB RMS、peak、mean absolute
- 三级归一化 Haar 小波的 `d1/d2/d3/a3` 子带能量、RMS、能量占比和系数数量

所有浮点计算显式使用 NumPy `float64`。规范化 JSON 使用 `sort_keys=True` 和紧凑分隔符生成 SHA-256；处理顺序来自 `np.arange`，不依赖字典遍历顺序、环境随机种子或系统时钟。样本生成只使用局部 `np.random.default_rng(seed)`。

## 2. 两代门限格式

### v1 扁平表

顶层包含 `"version": 1` 和 `table`/`rows`/`flat_thresholds`。每行用扁平字段表达时间区间、能量目标、容差和 `<band>_expected`、`<band>_tolerance`、`<band>_limit` 等列。

### v2 分片 JSON

顶层包含 `"version": 2` 和 `shards`。每个分片将时间范围放在 `range`，能量门限放在 `energy`，子带结构放在 `wavelet.bands`，并可携带 v1 无法表达的 `adjudication` 嵌套对象。

读取器先看 `version`/`format_version`/`schema_version`；没有版本头时按 `shards` 或 `table/rows/flat_thresholds` 字段集分派。主版本未知返回 `E_VERSION_UNSUPPORTED`。必需字段缺失、数值不能转 float/int、非有限值或区间非法返回 `E_FORMAT_CORRUPT`。区间排序后若 `start < previous.end`，返回 `E_TIMELINE_OVERLAP`，详情包含首个重叠的两个 segment。

归一化曲线保留 sample rate、帧长帧移、排序后的时间窗和四个子带定义，并计算 `threshold_hash`。

## 3. 双向迁移

- v1 → v2：把扁平列搬入 `wavelet.bands.<band>`；缺失的期望分布按 `segment_class` 应用 `silence` 或 `voiced/default` 默认值；缺失权重使用 `weight=1.0`。每个补全项写入 `migration_report.completed`，包含 JSON 指针路径、来源和值。
- v2 → v1：已知子带字段直接扁平化为 `<band>_expected`、`<band>_tolerance`、`<band>_weight`、`<band>_limit`；多个 RMS 上限额外按显式规则 `max across bands` 生成 `subband_limit`；无法表达的 `adjudication` 对象按 `discard_nested_adjudication_profile` 规则丢弃并记录到 `discarded`。

迁移后使用同一批 envelope block 比较原曲线和迁移曲线的能量门限布尔判定。样本中 1199 个对齐块全部一致；若出现不一致，流水线返回 `E_INCONSISTENT` 和首个偏差块。

## 4. 漂移裁决与双精度

每个块按最大重叠样本区间找到门限窗，记录：

- `block_hash`
- `threshold_hash`、`window_hash`
- `sample_interval=[start_sample,end_sample]`
- `energy_segment_id`
- `precomputed_hash = sha256(canonical_json(replay_input))`
- 完整 `replay_input.observed` 与紧凑 `replay_input.threshold`

float64 路径计算能量误差、子带绝对偏差、按容差归一化的贡献、加权贡献和 L1 分布距离。定点路径使用 `S=1,000,000` 将浮点值舍入为 `int64` 标量，再以整数比较复现同一分类。分类优先级固定为：

```text
DRIFT_ENERGY > DRIFT_SUBBAND > DRIFT_DISTRIBUTION > NO_DRIFT
```

`dominant_subband` 取加权归一化贡献最大的子带；同时提供 `dominant_excess_subband`，用于定位超配最多的高频/低频来源。若两档业务码不同，`detect_drift` 返回 `E_INCONSISTENT` 及首个帧位置。

## 5. 内容缓存与整体替换

缓存键的规范化输入包含：

```json
{
  "stage": "envelope|drift",
  "pcm_content_hash": "...",
  "threshold_version": "...",
  "threshold_hash": "...",
  "frame_length": 320,
  "frame_hop": 160,
  "upstream_envelope_hash": "...",
  "chunk_index": 0,
  "start_sample": 0,
  "end_sample": 320,
  "dsp_version": "haar3-energy-v1"
}
```

envelope 阶段使用 PCM/帧规格派生的 root hash 作为上游根；drift 阶段使用真实 `envelope_hash`。键相同直接复用 `entries/<key>.json`，键不同重新计算。缓存写入采用同目录临时文件加 `os.replace`，读取时验证 `checksum`。

当 PCM 哈希、门限哈希或帧规格变化时，生成新的 `generation`。替换计划输出：

- `changed_fields`
- `affected_chunks`
- `recompute_chunk_count`
- `recompute_sample_range`

新阶段的第一个快照提交时，`current.json` 原子指向新 generation；不会就地修改旧快照。旧的不可变快照文件仍保留在 `snapshots/`。

## 6. 四阶段状态机

阶段顺序为：

```text
envelope -> alignment -> drift -> report
```

每个阶段只有在全部块处理完成后才构造完整 body、计算内容校验和，并通过临时文件、fsync 和 `os.replace` 原子写入：

```text
<state>/
  current.json
  snapshots/000001-envelope-v001.json
  snapshots/000002-alignment-v001.json
  snapshots/000003-drift-v001.json
  snapshots/000004-report-v001.json
  cache/entries/<content-key>.json
  locks/commit.lock
```

`current.json` 保存每个阶段的 version、全局递增 revision、文件路径和 checksum。重启时先验证 current 与快照 checksum，再从最近阶段继续：

- envelope 已提交：直接加载全部块，不重算。
- envelope 未提交但部分 chunk cache 已原子落盘：阶段从头扫描，命中的已提交分块不重算，仅计算缺失键。
- alignment/drift/report 未提交：不写入半阶段快照；重启后从该阶段起点重建。

验收中的 `--crash-after 3` 调用 `os._exit(42)`，跳过 `finally` 和缓冲输出，近似 `SIGKILL` 的恢复边界。

## 7. 并发与资源预算

`CommitCoordinator` 同时使用：

- 进程内 `threading.Lock.acquire(blocking=False)`
- 跨进程 `fcntl.flock(LOCK_EX | LOCK_NB)`

锁被占用时后到者立即收到 `QCError(E_STAGE_BUSY)`，不会阻塞等待。`StateStore.commit_snapshot` 自身获取该锁，因此所有快照 current 指针替换均串行化。

`ResourceBudget` 可配置：

- `max_resident_blocks`
- `max_resident_bytes`
- `max_cache_entries`
- `max_parallel_chunks`

`ResourceGovernor.plan()` 先判断内存/驻留块预算，再判断并行数和缓存条目数。内存类超限返回 `E_MEMORY_BUDGET` 并进入 `serial_streaming`；否则资源类超限返回 `E_RESOURCE_LIMIT` 并进入 `serial`。严格调用方也可直接使用 `check_resident()` / `check_parallel()` 以异常形式取得对应业务码。

## 8. 自检与回放

`verify_serial_vs_oneshot()` 分别运行：

1. 逐块 `analyze_frame()` 的串行路径；
2. `sliding_window_view` 批量取帧、再走相同统计函数的整段路径。

它先比较每个 envelope block 的 canonical SHA-256，再比较每个 drift record 的 `business_code`、`sample_interval`、`precomputed_hash` 和 `dominant_subband`。发现差异时返回：

```json
{
  "ok": false,
  "code": "E_INCONSISTENT",
  "first_mismatch": {"position": 3, "field": "envelope_block"}
}
```

报表中的每条 event 都可由 `replay_event()` 验证 checksum 和 `precomputed_hash`，再从 `replay_input` 重算 float64 与定点结论。回放要求复现相同业务码和 `[start_sample,end_sample]`。

## 9. 本地验证摘要

最终代码上的真实验证结果：

- `python3 -m pytest --junitxml=/tmp/audio-qc-junit.xml -q`：JUnit 记录 22 项测试全部通过。
- `python3 -m audio_qc.cli acceptance --workdir artifacts/acceptance`：`QC_VERIFY_OK`。
- `python3 -m audio_qc.cli verify --sample-dir samples --report artifacts/acceptance/recovered-report.json`：v1/v2 均 `QC_VERIFY_OK`，回放 503 个事件成功。

机器可读结果：`artifacts/acceptance/acceptance_summary.json`。
