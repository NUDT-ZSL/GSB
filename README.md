# 本地 WAV 指纹检索与版权比对工具

这是一个独立实现的本地检索工具，不依赖旧工具代码、外部音频库、网络或真实站点。实现使用 Python 标准库 `sqlite3`/`wave` 与 NumPy，覆盖：

- WAV 头与检索任务严格校验
- 分帧频谱、峰值选择与指纹提取
- SQLite 指纹索引、哈希碰撞投票与时间偏移估计
- 定点得分判定与 float64 诊断
- 相似片段边界细化、重叠消解
- 分块增量缓存、失效原因记录
- 原子持久化、SIGINT/SIGTERM 边界退出与重启恢复
- 有界线程池并发与内存预算调度

## 快速开始

目标运行环境为 Windows + Python 3.11/3.12。当前交付在 Python 3.10.12 + NumPy 1.26.4 下完成实测；代码兼容 Python 3.10 至 3.12。

```powershell
py -3.12 -m pip install -r requirements.txt

# 生成固定种子 WAV、任务 JSON 与空状态目录
py -3.12 generate_samples.py --out sample_data

# 只校验，不创建索引/报告
py -3.12 -m audiofp.cli validate --task sample_data/task.json

# 执行检索
py -3.12 -m audiofp.cli run --task sample_data/task.json
```

主要输出：

- `sample_data/report.json`：完整报告、排序、片段、定点/浮点诊断、增量失效记录
- `sample_data/segments.json`：可直接下游消费的片段导出
- `sample_data/state/`：SQLite 索引、摘要有效的分块、版本清单、中断标记、逐段得分轨迹

`generate_samples.py` 的默认种子是 `20261125`。样本生成会重建输出目录，WAV 字节、任务 JSON 与样本清单可重复。

## 任务 JSON

规范字段如下；任务内相对路径均相对于任务 JSON 所在目录。

```json
{
  "schema_version": 1,
  "reference_files": ["audio/ref_a.wav"],
  "query_files": ["audio/query_a.wav"],
  "fingerprint": {
    "frame_length": 2048,
    "hop_length": 512,
    "band_boundaries_hz": [0, 250, 500, 900, 1500, 2400, 4000, 6000, 7800],
    "peak_neighborhood": 3,
    "peaks_per_frame": 4,
    "fan_out_frames": 8,
    "frequency_bucket_hz": 50.0,
    "pair_limit_per_peak": 16,
    "minimum_peak_hz": 50.0
  },
  "search": {
    "vote_window_frames": 20,
    "min_matches": 4500,
    "score_tolerance": 0.0005,
    "time_offset_tolerance_samples": 1024.0,
    "fixed_scale": 1000000,
    "relative_score_floor": 0.2
  },
  "concurrency_limit": 4,
  "memory_budget_bytes": 33554432,
  "chunk_frames": 64,
  "state_dir": "state",
  "report_path": "report.json",
  "segments_path": "segments.json"
}
```

参数口径：

- `frame_length`、`hop_length`：以输入 WAV 源采样点为单位；第 `n` 帧固定为 `[n*hop, n*hop+frame_length)`。
- `band_boundaries_hz`：严格递增的频率边界，单位 Hz；最后边界不得超过所有输入文件的最小奈奎斯特频率。
- `peak_neighborhood`：频率轴上局部峰值比较半径，单位为 FFT bin。
- `peaks_per_frame`：每帧全局保留的峰值数，峰值再按频带归类。
- `fan_out_frames`：锚点峰值向后配对的最大帧差。
- `frequency_bucket_hz`：频率哈希量化宽度。
- `pair_limit_per_peak`：每个锚点峰值最多生成的配对数。
- `minimum_peak_hz`：低于该频率的峰不参与指纹，避免 DC/超低频泄漏形成高碰撞键。
- `vote_window_frames`：偏移直方图平滑窗口和候选聚合窗口，单位参考帧。
- `min_matches`：候选片段最小碰撞匹配数。
- `score_tolerance`：定点累计得分与 float64 累计得分允许的绝对差。
- `time_offset_tolerance_samples`：时间偏移容差，单位参考采样点。
- `fixed_scale`：定点标度；最小匹配判定使用 `fixed_score_units >= min_matches * fixed_scale`。
- `relative_score_floor`：可选，`[0,1)`；候选定点得分低于本次查询最高候选的该比例时丢弃。它仍只依据定点结果。
- `memory_budget_bytes`：在途频谱缓冲与指纹中间量上限，单位字节；也可使用 `memory_budget_mb`。
- `chunk_frames`：候选窗口固定帧数。内存不足时只缩小提取/频谱分块，不改变帧长、频带精度和候选窗口。

为便于接入，校验器也接受若干常见别名，如 `references`/`refs`、`queries`、`frame_shift`、`vote_window`、`fixed_point_scale`、`max_concurrency`、`memory_budget` 等。规范输出仍使用上述字段。

## 校验与业务错误

所有非法输入通过 CLI 输出单行 JSON，例如：

```json
{"ok":false,"error":{"code":"E_NYQUIST","message":"频带边界越过输入文件的奈奎斯特频率"}}
```

校验发生在创建状态目录、索引和报告之前。非法输入退出码为 `2`，不会产生新索引或报告。

主要业务码：

| 业务码 | 含义 |
| --- | --- |
| `E_TASK_NOT_FOUND` / `E_JSON` / `E_SCHEMA` | 任务不存在、JSON 非法或字段缺失 |
| `E_PATH` / `E_DUPLICATE_FILE` | 输入路径非法或清单重复 |
| `E_WAV_HEADER` / `E_WAV_FORMAT` / `E_WAV_TRUNCATED` | WAV 头、PCM 格式或数据长度非法 |
| `E_SAMPLE_RATE` / `E_BIT_DEPTH` / `E_CHANNELS` | 采样率、位深或声道不符合要求 |
| `E_FRAME` / `E_BANDS` / `E_NYQUIST` | 帧参数或频带边界非法 |
| `E_PEAKS` / `E_TOLERANCE` / `E_SCALE` / `E_SEARCH` | 峰值、容差、定点标度或检索参数非法 |
| `E_CONCURRENCY` / `E_MEMORY` / `E_MEMORY_BUDGET_TOO_SMALL` | 并发或内存预算非法/不可行 |
| `E_STATE_DIR` / `E_OUTPUT_PATH` | 状态目录或输出路径非法 |
| `E_INDEX` / `E_CACHE_READ` / `E_TRACK_PROCESSING` | 索引、缓存或单曲处理运行时错误 |
| `E_INTERNAL` | 未分类内部异常 |

检索成功但某待检曲目没有任何参考偏移覆盖时，报告在 `unmatched_queries` 中给出：

```json
{
  "query_file": "audio/query_unrelated.wav",
  "code": "E_NO_REFERENCE_COVERAGE",
  "message": "待检曲目未被任何参考曲目在任一估计偏移覆盖"
}
```

## 算法与判定口径

### 1. 指纹提取

- 每帧乘 Hann 窗并计算 `rfft` 幅度谱，取 `log1p` 压缩。
- 按 `peak_neighborhood` 做局部极大值选择，再按强度排序保留每帧 `peaks_per_frame` 个峰值。
- 每个锚点峰值同后续 `fan_out_frames` 内峰值配对。哈希键包含：
  - 锚点/目标频带
  - 归一化帧间差（毫秒）
  - 锚点/目标频率桶
  - 增益不敏感的相对幅度桶
  - 峰值强度秩
- 指纹项同时保存锚点与目标帧的源采样起止点。帧边界只由 `frame_index * hop_length` 决定，不随并发、缓存分块或采样率分支改变。
- 不同采样率命中的时间偏移统一换算到秒后再映射到参考采样点，因此 16 kHz、44.1 kHz、48 kHz 输入可混合检索。

### 2. 索引与投票

- 指纹索引写入 `state/index.sqlite3`，`key` 上建立 SQLite 索引。
- 碰撞权重为 `1 + 0.5 / sqrt(该参考内同键出现次数)`，稀有键权重更高。
- 每个 `(查询锚点帧, 参考帧偏移)` 的碰撞被压缩成组，保存碰撞数、定点/浮点权重和、帧范围与权重分桶。这样既保留全部碰撞累计值，又避免把数百万条重复投票逐条落盘。
- 候选窗口固定由 `chunk_frames` 决定，与内存预算导致的提取分块大小无关。
- 偏移直方图按 `vote_window_frames` 平滑，保留局部峰；再按查询锚点相邻密度聚类为候选片段。

### 3. 定点与 float64 双路径

- 每条碰撞的 float64 权重参与参考累计。
- 每条碰撞的定点权重为 `max(1, round(weight * fixed_scale))`。
- 最终排序、最小匹配数、相对支持度过滤全部只使用定点结果。
- 报告保存：
  - `fixed_score_units` / `fixed_score`
  - `reference_float_score`
  - `score_difference`
  - 首次超过 `score_tolerance` 的原始碰撞序号 `first_tolerance_exceeded_at`
  - `fixed_rank` 与 `reference_float_rank`
  - 定点/浮点排序不一致候选列表 `fixed_float_rank_mismatches`
- 每段完整逐组得分轨迹原子写入 `state/traces/*.json`，报告通过 `score_trace_file` 引用。

### 4. 边界、偏移与重叠消解

- 候选起止边界取对应偏移模式下最密集的连续锚点簇，并按锚点密度向两侧扩展到簇边界。
- 两条时间轴都用整数采样点表示，并裁剪到各自文件长度内。
- 偏移估计是碰撞加权中位偏移；报告给出整数帧偏移及由该帧偏移换算的浮点参考采样偏移，因此二者换算差为 0，另以 `continuous_offset_residual_samples` 保留原始连续估计残差。
- 重叠消解按确定性串行规则执行：先按定点得分、匹配数、片段长度、曲目顺序、起止点排序；后到片段若与已接受片段在查询时间轴重叠则被抑制，并记录 `suppressed_by`。结果不依赖线程完成顺序。

## 增量失效与持久化

状态目录结构：

```text
state/
  index.sqlite3
  manifest.json
  interruption.json
  chunks/
    fp/<track_id>/<start>_<end>.json
    cand/<query_id>/<ref_id>/<start>_<end>.json
  stale/
  traces/
```

分块版本标识为规范化 JSON 的 SHA-256，包含：

- 分块依赖的源 PCM 内容摘要
- 帧区间与采样区间
- 指纹参数或检索参数
- 采样率与分块类型
- 候选窗口依赖的查询指纹分块版本、参考指纹版本

分块写入流程：

1. 在同目录创建临时文件。
2. 写入完整 JSON（含 `version`、`payload_sha256`、`payload`）并 `fsync`。
3. `os.replace` 整体替换目标文件。
4. 若目标已有不同版本，先把旧版本移到 `stale/`，不直接覆盖变更版本。
5. 重启时只接受版本匹配且 `payload_sha256` 校验通过的分块；损坏/半写临时文件被忽略。

增量规则：

- 新增待检曲目只计算新曲目的指纹与候选。
- WAV 尾部追加时，仅尾部依赖新上下文的指纹块和相应候选块失效，前缀块复用。
- 指纹参数变化时全部相关指纹块失效并逐条记录 `fingerprint_params_changed`。
- 参考指纹版本变化时，仅该参考相关候选失效并记录 `reference_changed`。
- 报告 `incremental.affected_intervals` 逐条给出阶段、曲目、帧区间、采样区间和失效理由。

## 并发、内存与中断

- 使用 `ThreadPoolExecutor(max_workers=concurrency_limit)`。
- 调度前按曲目清单顺序为每个提取任务预留内存额度；额度包含频谱矩阵和指纹中间量估计。
- 预算不足时任务等待，不改变 `frame_length`、`hop_length` 或频带边界。
- 提取分块可缩小到预算允许的帧数；候选窗口始终固定为 `chunk_frames`，因此资源变化不改变最终排序和边界。
- 任一任务异常时停止继续提交、取消未启动任务，并按曲目/分块声明顺序返回首个错误。
- 报告、片段导出、中断标记、manifest、得分轨迹均采用同目录临时文件 + `os.replace`。

信号与退出码：

- `SIGINT`：在下一个分块边界停止，写 `interruption.json`，退出码 `130`
- `SIGTERM`：在下一个分块边界停止，写 `interruption.json`，退出码 `143`
- `SIGKILL`/强制终止：不承诺中断标记或退出报告；已完整持久化且摘要有效的分块可在重启时复用

测试专用环境变量：

- `AUDIOFP_INTERRUPT_AFTER_CHUNKS=N`：在第 N 个分块边界触发与 SIGINT 相同的受控中断
- `AUDIOFP_TEST_CHUNK_DELAY_MS=N`：每个分块边界增加测试延迟，便于发送真实信号

## 测试

```powershell
py -3.12 -m unittest discover -v
py -3.12 verify_acceptance.py
```

`unittest` 覆盖任务/WAV 校验、无副作用错误路径、分块/并发指纹确定性、定点容差首次越界、原子版本冲突、缓存增量、边界中断恢复等。

### 实际验收结果

验收环境：Python 3.10.12、NumPy 1.26.4、固定种子 `20261125`。共生成 7 个 WAV，覆盖 16 kHz、44.1 kHz、48 kHz、16bit 单声道。

`python -m unittest discover -v`：**20 项通过，0 失败**。

固定种子连续生成两次得到 9 个文件（7 个 WAV + 任务 JSON + 样本清单），逐文件 SHA-256 完全一致，状态目录为空。

`python verify_acceptance.py`：**passed**，总耗时约 169 秒，峰值内存约 153 MB。结果文件见 `artifacts/acceptance_results.json`。

| 验收项 | 结果 |
| --- | --- |
| 基准全量检索 | 5 个片段；A 因局部插入分为 2 段，B 为 2 段，C 为 1 段；无关噪声返回 `E_NO_REFERENCE_COVERAGE` |
| 并发上限 1 vs 4 | 片段边界、偏移、定点得分、全局排序完全一致 |
| 内存预算 32 MiB vs 700,000 B | 结果完全一致；仅提取分块变小 |
| 新增待检曲目增量 vs 全新全量 | 44 个既有指纹块复用，新增 18 块；5 个片段完全一致 |
| WAV 尾部追加 | 61 个指纹块复用，仅重算 3 个尾部块；增量与全新全量完全一致 |
| 指纹参数变化 | 逐条记录 `fingerprint_params_changed`；结果与全新全量一致 |
| 分块边界中断 | 退出码 130，无报告，中断标记存在；恢复后结果一致 |
| 真实 SIGTERM | 退出码 143，标记 `signal=SIGTERM`，恢复后结果一致 |
| SIGKILL | 已落盘分块摘要全部有效（坏摘要 0），重启后结果一致 |
| 非法输入 | 返回 `E_NYQUIST`，退出码 2，不创建状态目录与报告 |

基准片段摘要：

| 排名 | 待检 → 参考 | 匹配数 | 定点得分 | float64 得分 | 首次越界碰撞 |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | query_a → ref_a（插入前） | 40349 | 45967.637725 | 45967.637872 | 5470 |
| 2 | query_a → ref_a（插入后） | 36919 | 42421.184243 | 42421.183398 | 16329 |
| 3 | query_b → ref_b | 20564 | 22775.635540 | 22775.635720 | - |
| 4 | query_b → ref_b | 19354 | 21972.038411 | 21972.038476 | - |
| 5 | query_c → ref_c | 4737 | 6389.255582 | 6389.256119 | 4342 |

基准任务未出现定点/浮点排序不一致，`fixed_float_rank_mismatches` 为空；该列表与逐段首次越界位置仍会按任务实际结果生成。

样本文件、状态目录、报告和验收中间 WAV 均被 `.gitignore` 排除，不会进入版本控制。
