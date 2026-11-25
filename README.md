# 电话会议语音端点检测（VAD）规则引擎

这是一个完全本地、确定性的 VAD 规则引擎实现，面向两代规则配置互操作、多人说话端点漂移、内容寻址缓存、四阶段快照恢复等场景。所有 PCM、规则和真值均由固定种子脚本生成，不访问网络、不使用 GPU 或容器。

## 环境与快速开始

要求 Linux/WSL Ubuntu、Python 3.10+、NumPy，Python 内置 `sqlite3`。

```bash
python3 --version
python3 -c 'import numpy, sqlite3; print(numpy.__version__, sqlite3.sqlite_version)'
python3 -m pip install -r requirements.txt

python3 generate_samples.py --out data --seed 20261126
python3 main.py --reset-state --seed 20261126
python3 main.py --replay-report reports/drift_report.json --data-dir data
```

`main.py` 默认会：

1. 若无样本则调用生成器，种子固定为 `20261126`。
2. 运行强制终止/恢复和缓存整体替换演练。
3. 执行包络估计、规则互迁对齐、漂移裁决、报表导出四阶段。
4. 写出 `reports/run_report.json`、`reports/drift_report.json`、`reports/drift_report.csv`、`reports/verification.json`、`reports/recovery_report.json`。

运行测试：

```bash
python3 -m unittest discover -v
```

退出码为 `0` 表示成功；可预期的业务失败以 `E_*` 业务码输出并返回 `1`。

## 生成数据

`generate_samples.py` 输出：

```text
data/
  manifest.json
  pcm/session_001.pcm ...           # 16 kHz, mono, PCM s16le
  truth/session_001.jsonl ...       # 20 ms 逐帧真值
  meta/session_001.json ...         # utterance 与 gain jump 元数据
  rules/v1/session_001.json ...     # 无版本字段头的扁平 v1
  rules/v2/session_001/
    manifest.json
    metadata.sqlite3
    shards/shard_000.json ...
```

真值字段包括：

- `frame_index`
- `sample_start`, `sample_end`（右开区间）
- `is_speech`
- `speaker_id_true`
- `noise_floor_true`
- 附加的 `utterance_id`, `segment_start_ms`, `segment_end_ms`

当前样本集是轮次式多人对话，每个 20 ms 真值帧最多一个真实说话人；信号中包含多人音色、间歇静音、可配置平稳底噪和若干增益跳变点。

## 规则格式与迁移

### v1

单文件 JSON，无 `format`/版本头。通过字段集合分派：

```json
{
  "session_id": "session_001",
  "sample_rate": 16000,
  "frame_ms": 20,
  "hop_ms": 10,
  "noise_gate": 0.00432,
  "speakers": ["spk_a", "spk_b", "spk_c"],
  "rules": [
    {
      "speaker_id": "spk_a",
      "start_sec": 0.35,
      "end_sec": 1.25,
      "energy_threshold": 0.0305,
      "min_silence_ms": 200
    }
  ]
}
```

`end_sec` 可省略；省略时区间止于同说话人的下一条规则。

### v2

分片 JSON 保存阈值/静音时长，SQLite 元数据库与分片中的元数据字段必须一致。v2 支持绝对毫秒时间、`speaker_id`、`rule_generation`、`scope`、`tombstone`。

### 互迁

- `v1 -> v2`：生成绝对毫秒字段、分片索引、`rule_generation=1`、`scope=speaker`、`tombstone=false`，并为每个补全字段写 `migration_provenance`。
- `v2 -> v1`：显式丢弃无法表达的 `rule_generation != 1`、作用域墓碑及不支持作用域，每条丢弃记录带业务码和原因。
- 迁移后对区间边界及区间中点逐说话人检查门限、最小静音和标签语义，不一致抛出 `E_STATE_INCONSISTENT`。

## 包络与漂移

包络块边界只由样本索引决定：

```text
block i = [i * frame_shift_samples, i * frame_shift_samples + frame_length_samples)
```

默认帧长 20 ms（320 样本）、帧移 10 ms（160 样本），仅输出完整帧。块统计量包括归一化短时能量、RMS、过零率、过零数、峰值和定点 RMS。

漂移裁决比较：

1. v1 对齐基线曲线与估计 RMS；
2. v1/v2 规则曲线差；
3. 带最小静音挂起的 v1/v2 端点标签差。

事件分类为：

- `D_ENDPOINT_SHIFT`
- `D_ENERGY_DRIFT`
- `D_RULE_CURVE_DRIFT`

每条事件记录包络块、样本区间、规则版本、预计算输入哈希、最大贡献说话人活动段、短时能量子带及同 utterance 内最近增益跳变点。事件可脱离流水线独立回放：重新读取 PCM、规则与真值，重算块哈希、曲线、标签、漂移类别和样本区间。

浮点路径使用 float64；定点路径将值缩放到 `1_000_000` 的整数。若两条路径的分类或端点标签不同，返回 `E_PRECISION_MISMATCH` 并给出首个块位置。

## 缓存、快照与资源

包络块缓存键包含：

- PCM 内容 SHA-256
- v1/v2 规则版本
- 帧长、帧移、采样率
- 上游包络块源哈希

缓存命中直接复用；规则或音频修订导致键变化时重新计算，并以完整快照替换旧包络快照，不做增量修补。报告同时列出受影响块和复用块。

四阶段为 `envelope -> align -> drift -> report`。每个阶段只有在全部 utterance 完成后才提交快照：

- 全局单调版本号；
- 完整 `utterance_id` 集合；
- 内容 SHA-256；
- 前序版本号；
- SQLite 原子事务发布缓存与快照索引。

快照负载先以临时文件落盘并 `fsync`，再通过 SQLite 事务发布。写入文件后崩溃产生的无引用负载会在下次启动清理；未提交缓存项在启动时删除。已提交阶段不会重算。

资源优先级：

1. 包络驻留内存、缓存条目数为硬上限，超限返回 `E_RESOURCE_LIMIT`。
2. 并行分块数超过软上限时降级为串行并记录 `R_DEGRADED_PARALLEL_TO_SERIAL`。
3. 严格资源模式下，并行软上限也转为 `E_RESOURCE_LIMIT`。

规则快照和漂移裁决提交共用 Linux `flock` 非阻塞闸门；冲突立即返回 `E_STAGE_BUSY`。

## 业务码

| 业务码 | 含义 |
|---|---|
| `OK` | 运行成功 |
| `E_VERSION_UNSUPPORTED` | 未知规则/分片/元数据版本 |
| `E_FORMAT_CORRUPT` | 字段缺失、结构错误、数据库结构错误 |
| `E_PARSE_ERROR` | 数值或 JSON 不可解析 |
| `E_TIMELINE_OVERLAP` | 同说话人同代规则时间轴重叠 |
| `E_STAGE_BUSY` | 规则快照或漂移裁决提交冲突 |
| `E_RESOURCE_LIMIT` | 内存、缓存或严格并行预算超限 |
| `E_STATE_INCONSISTENT` | 快照/迁移/串行整段一致性失败 |
| `E_PRECISION_MISMATCH` | float64 与定点裁决不一致 |
| `E_REPLAY_MISMATCH` | 独立回放与报表不一致 |

另有迁移码 `M_*`、缓存码 `CACHE_*`、阶段码 `S_STAGE_*`、漂移码 `D_*`。

## 自检与恢复验证

流水线内建：

- 分块串行包络与整段向量化包络的漂移类别/样本区间一致性检查；
- 每个事件的全量独立回放；
- 迁移语义逐点检查；
- float64/定点全块裁决检查。

恢复演练覆盖：

- 包络阶段注入崩溃：确认快照为 0、存在未提交缓存；重启后未提交缓存清零并从版本 1 重建。
- 已提交 `envelope/align` 后重启：只新增 `drift/report` 版本，前两阶段复用原校验和。
- 缓存复用：仅改变非缓存键配置，1977 个块全部命中。
- 规则修订：快照整体替换，受影响块重算、未受影响块复用。

