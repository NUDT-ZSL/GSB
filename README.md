# Broadcast Loudness Delivery Pipeline

面向游戏配音与播客交付的确定性本地流水线。工程只依赖 Python 3.11+
标准库与 `numpy`，不访问网络，不依赖音频库、外部服务或容器。

工程提供：

- 固定种子素材、任务、两代口径快照、SQLite 元数据和异常注入清单生成。
- WAV、任务 JSON、v1/v2 口径快照的解析与输入级 `E_xxx` 校验。
- v1 相对秒到绝对逻辑毫秒的换算，以及跨两代的统一判据视图。
- 门限式加权响度、短期响度范围、动态范围预算和压限/增益建议。
- 有界线程池、解码缓冲预算和采样数据预算的双重准入控制。
- 摘要校验的分块持久化、安全边界中断、退出码和断点恢复。
- 有理数比重采样、16/24 位封装、24 位小端三字节编码。
- 音频文件组与 JSON 报告的同版本事务提交及失败回滚。

## 环境与快速开始

在 Windows PowerShell 中：

```powershell
python -m pip install numpy
python -m audio_pipeline self-check
python -m audio_pipeline seed --workspace demo
python -m audio_pipeline run --task demo/task.json --workspace-root demo
```

也可直接生成并运行：

```powershell
python -m audio_pipeline demo --workspace demo --force
```

固定种子演示包含 5 个输入，其中 2 个故意非法、3 个可测量，另有 3 条
异常注入。命令返回码 `3` 表示流水线已完成，但存在已记录的输入级业务
错误或口径/异常降级，这是演示数据的预期结果。音频和报告位于
`demo/output/`。

独立种子脚本：

```powershell
python scripts/generate_fixtures.py --workspace demo --force
```

运行测试：

```powershell
python -m unittest discover -s tests -t . -v
```

## 工程结构

```text
audio_pipeline/
  config.py       任务解析、域校验、输入级错误降级
  wavio.py        RIFF/WAVE PCM 读取、16/24 位写码、有理数比重采样
  snapshots.py    v1/v2 快照摘要校验、SQLite 交叉校验、统一判据合并
  measurement.py  滑窗能量、双级门限、分块测量、停止边界
  decision.py     动态范围预算、增益与压限建议
  scheduler.py    声明顺序准入、双预算、有界线程池、首个错误取消
  state.py        SQLite 分块索引、摘要校验、恢复和中断标记
  output.py       同目录暂存、版本判定、组提交和回滚
  pipeline.py     端到端编排、确定性报告
  seed.py         固定种子生成器
scripts/
  generate_fixtures.py
tests/
```

所有运行目录必须位于任务工作区内。默认输出目录为 `output/`，状态目录为
`state/`；暂存文件始终在最终文件的同目录创建。

## 任务描述

`task.json` 的关键字段如下：

```json
{
  "task_id": "broadcast-delivery-demo",
  "allowed_sample_rates": [44100, 48000, 96000],
  "allowed_bit_depths": [16, 24],
  "measurement": {
    "window_ms": 100,
    "hop_ms": 50,
    "partial_window_policy": "zero_pad",
    "relative_gate_lu": -10.0,
    "absolute_gate_lufs": -70.0,
    "dynamic_range_min_db": 4.0,
    "dynamic_range_max_db": 12.0,
    "target_lufs": -23.0,
    "compression_threshold_lufs": -18.0,
    "compression_ratio": 2.0,
    "target_sample_rate": 48000,
    "output_bit_depth": 24,
    "max_workers": 3,
    "decode_buffer_bytes": 1048576,
    "sample_buffer_bytes": 16777216,
    "checkpoint_windows": 4,
    "decode_block_frames": 4096
  },
  "inputs": [
    {
      "id": "promo",
      "canonical_id": "program/promo",
      "path": "media/promo.wav",
      "layout": "stereo",
      "channels": 2,
      "weights": [0.5, 0.5],
      "declared_sample_rate": 48000,
      "declared_bit_depth": 24
    }
  ],
  "snapshots": {
    "v1": {
      "path": "criteria/v1.json",
      "sha256": "<64 hex>",
      "expected_entries": 10,
      "logical_origin_ms": 500
    },
    "v2": {
      "database": "criteria/v2.sqlite3",
      "sha256": "<64 hex>",
      "shard_root": "criteria/v2"
    }
  },
  "anomalies": "anomalies.json"
}
```

支持别名：`concurrency`/`max_workers`、
`in_flight_decode_bytes`/`decode_buffer_bytes`、
`max_sample_bytes`/`sample_buffer_bytes`、
`target_bit_depth`/`output_bit_depth`。

输入级非法条目只产生该输入的 `E_xxx` 结果，不会终止其余输入。任务级
配置、快照声明或输出目录非法则终止运行。

## 两代口径与合并

### v1

单文件 JSON，按条目记录：

```json
{
  "logical_origin_ms": 0,
  "expected_windows": 2,
  "entries": [
    {
      "record_id": "v1-promo-window",
      "canonical_id": "program/promo",
      "criterion": "window",
      "generation": 1,
      "effective_seconds": 0.25,
      "window_ms": 120,
      "hop_ms": 60,
      "policy": "zero_pad"
    }
  ]
}
```

绝对生效毫秒为：

```text
task v1.logical_origin_ms + file logical_origin_ms + round(effective_seconds * 1000)
```

### v2

分片 JSON 是记录正文，`v2.sqlite3` 是元数据索引。SQLite 至少包含：

```sql
snapshot_shards(shard_id, path, sha256, expected_windows,
                generation, logical_origin_ms)
criteria(record_id, shard_id, canonical_input_id, criterion,
         generation, effective_ms, tombstone, suggestion_rule, payload_json)
```

加载时同时校验：

- v1 文件摘要和 v2 SQLite 文件摘要必须由任务显式声明。
- 每个 v2 分片的数据库摘要、文件实际摘要和声明条目/窗口数。
- JSON 声明的预期条目数和数据库中的预期条目数。
- SQLite 元数据与分片正文中的规范输入标识、判据、代数、绝对时刻和墓碑。

任一项失效返回 `E_DIGEST`/`E_SNAPSHOT` 并跳过失效来源；其余有效来源
继续参与合并。若两代均无有效来源，运行以 `E_DIGEST` 终止。

### 裁决

同一 `(canonical_id, criterion)` 按以下顺序取最高记录：

```text
generation, effective_ms, tombstone, ingestion sequence
```

因此新代数优先；同代较晚生效优先；同刻墓碑优先。跨 v1/v2 且阈值或窗口
签名不同时，逐条生成冲突项，包含双方完整 `source`（文件、分片、索引、
记录 ID、摘要）和获胜来源。

`measurement_revision` 只包含窗口、hop、尾窗策略和双级门限；动态范围、
目标响度、压限规则属于 `decision_revision`。因此只修改建议规则时，
测量分块键不变，已完成测量直接复用，仅重算判定。

## 响度与预算算法

1. 窗口以源采样率换算为整数帧：`round(ms * sample_rate / 1000)`，起点和
   终点严格落在采样点上。
2. 每声道计算窗口内均方能量，声道能量按声明权重加权后除以权重和。
3. 单窗响度为 `-0.691 + 10 * log10(energy)`。
4. 绝对门限先筛一次；对通过窗口的能量均值算出相对门限
   `abs_gated_loudness + relative_gate_lu`，再筛第二次。
5. 积分响度只对最终通过双门的窗口能量取算术平均后转 LUFS。
6. 短期峰值/谷值为通过绝对门限的有限窗响度最大/最小值，动态范围为
   两者之差。
7. 全零输入返回 `integrated_lufs = null`、`silent = true` 和
   `E_SILENT`；有能量但未过门限返回 `E_BELOW_GATE`。两种情况均不会
   产生 `-Infinity` 等非法 JSON。

尾窗策略：

- `zero_pad`：补零到完整窗口长度，按完整长度求均值。默认值。
- `shorten`：只对剩余有效样本求均值，窗口记录为 `partial`。
- `drop`：丢弃不足一窗的尾段。

动态范围判定为 `within`、`below_min`、`above_max` 或 `unavailable`。
建议动作包括 `none`、`gain_up`、`gain_down`、`limit`、
`gain_and_limit`：

```text
gain_db = target_lufs - integrated_lufs
limiter = compression.enabled
          and (integrated_lufs > compression.threshold_lufs
               or dynamic_range > max_db)
```

报告同时给出目标、上下界、压限阈值、压缩比、原因列表和决策修订号。

## 并行调度与双预算

- 使用 `ThreadPoolExecutor(max_workers=N)`，主调度器按输入声明顺序逐个
  申请预算后才提交任务。
- 解码预算按每次解码块的最坏临时分配保守预留。
- 采样预算覆盖源样本和重采样/量化过程的保守峰值预留。
- 任一预留超过总预算时该输入返回 `E_BUDGET`；其余输入继续。
- 预算暂不满足时等待已运行任务释放，不降低窗口、权重或采样精度。
- 首个不可恢复错误会设置取消事件；运行中任务只在输入边界或窗口分块
  边界退出，不再提交后续输入。已完成结果保留，未处理输入标记为取消。

## 分块持久化与恢复

测量按照 `checkpoint_windows` 写入 `state/chunks/<cache-key>/`：

1. 在实际文件同目录写临时文件并 `fsync`。
2. 原子替换为正式分块。
3. 将分块摘要、声明窗口数、起止窗口和路径提交到
   `state/state.sqlite3`。

恢复时只读取数据库索引中连续、内容摘要正确、声明窗口数等于实际窗口
数的分块。缺失、摘要不符、窗口数不符或索引不连续时，回退到最近有效
前缀，删除该前缀之后的分块并重做。未被 SQLite 引用的孤儿文件不会被
信任。

SIGINT/SIGTERM 处理器只设置停止标志；工作线程或输入边界检查后：

- 不在输入或窗口分块边界完成任何部分提交；
- 已提交分块保留；
- 写 `state/interrupted.json`；
- 写 `state/partial_report.json`；
- SIGINT 退出 `130`，SIGTERM 退出 `143`。

恢复命令：

```powershell
python -m audio_pipeline run --task demo/task.json --workspace-root demo --resume
```

测试钩子 `--simulate-interrupt-after-chunks N` 会在第 N 个分块提交后模拟
SIGTERM，便于确定性验证恢复。

## 输出与原子提交

- 采样率按 `target/source` 约分后的有理数相位位置重采样；输出帧数为
  `round(source_frames * target / source)`，误差不超过 1 个目标采样点。
- 16 位输出为有符号小端两字节；24 位输出为小端三字节
  `[低, 中, 高]`。
- 输出报告包含预期帧数、实际目标采样率、位深和 `output_version`。
- 提交前读取旧报告的版本和旧 WAV 的实际帧数。
- 版本相同且帧数、输出集合一致时跳过替换。
- 版本不同时，先在同目录写完所有 WAV 和报告临时文件，再备份旧文件，
  最后以报告为提交点替换。任何替换失败都会删除新文件并恢复全部旧文件。
- 成功的版本切换会一并清理不再属于当前清单的旧 WAV，失败时同样恢复。
- 运行期不可恢复错误不会替换旧交付物，只写状态目录中的部分报告。

测试时可设置 `AUDIO_PIPELINE_FAIL_COMMIT=write|after_audio|report` 注入
提交失败。

## 报告

`output/report.json` 包含：

- 顶层：状态、退出码、任务/输出修订号、汇总、目标参数、调度器峰值。
- `criteria`：快照摘要、统一判据来源、跨代冲突、快照问题。
- `anomaly_injections`：每条注入项的种类、目标、期望/观测码、状态和位置。
- `inputs`：严格按任务声明顺序，含输入帧数、窗口数、门限窗数、加权响度、
  动态范围、建议动作、业务码、诊断、输出文件与帧数。
- `first_error`、`interruption`：首个不可恢复错误或安全边界中断信息。

## 错误码与退出码

| 错误码 | 含义 |
|---|---|
| `E_CONFIG` | 任务/输出配置非法 |
| `E_INPUT` | 输入描述非法或缺失 |
| `E_INPUT_MISSING` | 音频文件不存在 |
| `E_WAV` | WAV 容器或采样数据损坏 |
| `E_SR` | 采样率不支持或与声明不符 |
| `E_BIT_DEPTH` | 位深不是 16/24 或与声明不符 |
| `E_CHANNELS` | 实际声道数与布局不一致 |
| `E_LAYOUT` | 布局声明非法 |
| `E_WEIGHTS` | 权重非有限、负数、长度不符或权重和为零 |
| `E_WINDOW` | 窗口/hop/尾窗策略非法 |
| `E_GATE` | 门限或压缩参数越界 |
| `E_DRANGE` | 动态范围上下界非法 |
| `E_BUDGET` | 单输入或调度预算非法 |
| `E_SNAPSHOT` | 快照正文/元数据不一致 |
| `E_DIGEST` | 摘要缺失、不符或窗口数不符 |
| `E_SQLITE` | v2 SQLite 无法读取 |
| `E_ANOMALY` | 异常清单非法或注入未检出 |
| `E_IO` | 本地文件操作失败 |
| `E_INTERRUPTED` | 在安全边界收到终止信号 |
| `E_RUNTIME` | 意外的运行期失败 |
| `E_SILENT` | 所有窗口均为数字静音 |
| `E_BELOW_GATE` | 有能量但没有窗口通过门限 |

进程退出码：

| 退出码 | 含义 |
|---|---|
| `0` | 全部输入完成，无降级 |
| `2` | 任务/输入级解析入口错误 |
| `3` | 已完成，但存在被拒输入、摘要降级或异常注入 |
| `4` | 运行期、提交或配置/快照致命错误 |
| `130` | 在安全边界收到 SIGINT |
| `143` | 在安全边界收到 SIGTERM |

演示数据固定包含两条非法输入和一条损坏分片，因此 `3` 是预期成功退出码。

## 实际验证

本环境实际执行结果见 `VALIDATION.md`。测试覆盖口径合并、墓碑、跨代冲突、
分块尾窗、权重、并行预算、异常追踪、决策复用、中断恢复、损坏分块回退、
16/24 位输出、原子回滚和运行期首错处理。
