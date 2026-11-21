# 通话静音段能量事件归因应用

这是一个完全本地、确定性的通话静音段能量归因应用。它用固定种子合成 16 kHz 单声道 PCM 通话，执行四阶段批处理，并把每个成功阶段发布为带单调版本号和内容校验和的不可变快照。

核心能力：

- 按样本索引精确切分 20 ms 帧，计算短时能量与过零率，并与帧级观测逐帧核对。
- 用非语音低分位窗口拟合噪声底，识别持续电平台阶。
- 把静音段归因为 `stationary_floor`、`device_step`、`crosstalk_residue`。
- 合并同归因相邻静音段，生成事件时间轴和通话级画像。
- 强制终止后从最后完整提交的阶段恢复，丢弃未提交中间状态。
- 配置、真值与 PCM/观测变化时按通话和帧区间定向失效，整体发布新快照。
- 每个事件可沿特征版本、噪声底版本、归因配置版本和快照版本重放。
- 跨阶段 call_id、失效集合、事件时间轴、画像统计和来源链自检。

不使用 Docker、数据库服务、网络模型或外部音频资源。

## 环境

目标环境为 Windows + Python 3.10 及以上 + NumPy。当前实现只使用标准库与 NumPy，没有 POSIX 专用接口；原子文件替换使用 `os.replace`。

Windows PowerShell 准备：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m mute_attribution preflight --data-dir data --store runtime\store
```

`preflight` 会输出 Python、NumPy、采样率、帧长、数据目录与存储目录可写性检查。若尚未生成样本，先执行下一节的 `generate`。

当前工作区的实际执行环境为 Python 3.10.12、NumPy 1.26.4。应用没有使用容器。

## 快速开始

生成固定种子样本：

```bash
python -m mute_attribution generate --data-dir data --seed 20261122 --calls 3
```

完整运行：

```bash
python -m mute_attribution run \
  --data-dir data \
  --store runtime/store \
  --config configs/default.json \
  --output runtime/run_report.json
```

跨阶段一致性自检和事件重放：

```bash
python -m mute_attribution consistency --data-dir data --store runtime/store
python -m mute_attribution replay --data-dir data --store runtime/store --event-id call_0001-E001
```

强制终止恢复验证：

```bash
python -m mute_attribution run --data-dir data --store runtime/crash_store --crash-at noise_floor:1
python -m mute_attribution run --data-dir data --store runtime/crash_store
```

第一次命令在噪声底阶段处理完第 1 个通话后调用 `os._exit(137)`。bash 中退出码为 137，PowerShell 中可用 `$LASTEXITCODE` 查看。第二次命令会清理待处理目录并从 `features` 提交前缀恢复。

真值修正和 PCM/观测替换：

```bash
python -m mute_attribution truth-edit --data-dir data --call-id call_0001 --start-frame 216 --end-frame 228 --kind speech
python -m mute_attribution replace-pcm --data-dir data --call-id call_0002 --gain 1.1
```

`replace-pcm` 同步重算对应帧观测中的 `energy`、`zero_crossing_rate` 与 `noise_floor_true`，用于模拟“替换局部 PCM 观测”。

完整验收：

```bash
python -m mute_attribution acceptance --work-dir runtime/acceptance --artifact-dir artifacts
```

报告写入 `artifacts/acceptance_report.json`。

## 生成数据

默认生成三次通话：

| call_id | 时长 | 帧数 | 静音段 | 平稳 / 突变 / 串入 |
|---|---:|---:|---:|---:|
| call_0001 | 4800 ms | 240 | 5 | 3 / 1 / 1 |
| call_0002 | 6000 ms | 300 | 7 | 5 / 1 / 1 |
| call_0003 | 7000 ms | 350 | 8 | 6 / 1 / 1 |

目录：

```text
data/
  manifest.jsonl
  sample_metadata.json
  calls/call_0001.pcm
  observations/call_0001.jsonl
  truth/call_0001.json
  profiles/call_0001.json
```

PCM 为小端有符号 16 位、16 kHz、单声道裸数据。时间线由固定种子生成，并包含缓慢底噪漂移、设备底噪阶跃和低频串入残留。

`manifest.jsonl` 字段：

```json
{"call_id":"call_0001","duration_ms":4800,"seed":20261122,"sample_rate":16000,"frame_ms":20,"noise_profile_ref":"profiles/call_0001.json"}
```

观测 JSONL 每帧一行，字段为：

```text
frame_index, sample_start, sample_end, noise_floor_true, is_speech, energy, zero_crossing_rate
```

`sample_end` 为开区间。短时能量定义为归一化 int16 样本的均方值；过零率定义为帧内符号变化次数除以帧样本数。每帧 320 个样本。

真值文件保存连续 `speech` / `silence` 段、帧数、采样率和一个内容哈希 `revision`。归因阶段只读取真值边界，不使用生成器内部的场景名称。

## 四阶段语义

### 帧级特征

- 帧起点为 `frame_index * 320`，不依赖浮点时间累计。
- 计算值与观测逐帧比较，帧数不一致或在某帧的能量/过零率/样本边界不一致时返回 `E_FRAME_MISMATCH`。
- 错误上下文包含 `call_id`、`computed_frame_count`、`observed_frame_count` 和 `mismatch_frame`。
- 特征记录包含 `frames_checksum`；确定性样本在同一输入下产生相同规范 JSON 字节。

### 噪声底拟合

- 默认窗口 25 帧、步长 5 帧、低分位 0.2。
- 窗口只从非语音帧取低分位，避免长语音抬高估计；窗口内非语音样本不足时使用邻近有效窗口。
- 语音帧的曲线由前后非语音估计线性插值。
- 台阶检测以全通话非语音平台电平的稳健中位数为基线，默认电平差阈值为 8 dB。
- 段内输出估计值、离散度和覆盖窗口记录。
- 总帧数小于窗口返回 `E_WINDOW_INVALID`；步长为 0 返回 `E_STEP_INVALID`；两者都带 `call_id`。

### 静音段归因

每个连续静音段计算：

- 平均能量相对拟合噪声底的平均偏离 `deviation_db`；
- 段内能量变异系数 `volatility`；
- 平均过零率 `zcr_mean`；
- 噪声底段内跨度 `floor_span_db`；
- 是否包含噪声台阶边界 `overlaps_noise_step`。

规则按 `configs/default.json` 中的显式顺序执行：

1. 高波动或“较高过零率 + 高偏离”归因为 `crosstalk_residue`；
2. 跨越噪声台阶或底噪段内跨度显著归因为 `device_step`；
3. 其余归因为 `stationary_floor`。

所有规则输出标签必须属于 `controlled_labels`，否则返回 `E_LABEL_INVALID`。真值帧数跨出特征帧边界，或静音区间包含观测语音帧，返回 `E_SEGMENT_SPLIT`。

### 画像聚合

- 同标签分段若间隔不超过 `min_gap_frames` 且间隔内无语音帧，则合并为事件。
- 事件必须升序、互不重叠并落在通话时长内；分别返回 `E_EVENT_RANGE` 与 `E_EVENT_OVERLAP`。
- 画像包含各标签总时长、事件数、总事件数、静音总时长和噪声突变次数。
- 事件和画像统计都可以由分段明细独立重算。

## 快照与恢复

存储布局：

```text
runtime/store/
  state.json
  stages/features/versions/v000001/snapshot.json
  stages/noise_floor/versions/v000001/snapshot.json
  stages/attribution/versions/v000001/snapshot.json
  stages/profile/versions/v000001/snapshot.json
  archive/<stage>/v000001/snapshot.json
  work/<stage>/<call_id>.json
```

`state.json` 是唯一可见性指针。提交顺序：

1. 完成阶段内全部通话，将每个中间结果原子写入 `work/<stage>/`；
2. 写入完整不可变快照目录；
3. 归档旧活动版本和所有下游旧版本；
4. 原子替换根目录 `state.json`，同时发布本阶段并移除下游活动指针。

因此一个阶段未完成全部通话时不会出现在 `state.json`。待处理目录不参与下游读取，重启时整体删除。

每个快照包含：

- 阶段名与单调递增版本号；
- 完整 `completed_call_ids`；
- `all_calls_complete: true`；
- 阶段配置和每通话依赖签名；
- 完整记录和内容 SHA-256；
- 受影响、复用、重算通话集合及受影响帧区间/分段 ID。

读取时重新计算快照 SHA-256 并校验 call_id 集合。指针缺失、内容损坏、部分快照越权发布都返回 `E_SNAPSHOT_CORRUPT`。试图发布不大于当前活动版本的回退返回 `E_SNAPSHOT_ROLLBACK`。

恢复行为：

- 只从 `state.json` 中连续已提交前缀恢复。
- 丢弃所有 `work/` 中间文件。
- 已提交阶段及其通话记录不重算。
- 未提交阶段从阶段起点重建。
- 恢复报告列出 `resumed_from`、重启前已提交阶段、重建阶段和被丢弃目录。

## 定向失效

每通话记录保存显式依赖：

- 特征：特征配置 revision、PCM/观测/清单内容指纹；
- 噪声底：来源特征记录校验和、噪声配置 revision；
- 归因：来源噪声记录校验和、真值内容指纹与 revision、归因配置 revision；
- 画像：来源归因记录校验和、画像配置 revision、时长、受控标签集合。

失效示例：

- 修改真值 220–228 帧：特征、噪声底复用；只重算 `call_0001` 归因，帧区间报告为 `[220,228)`；画像只重算 `call_0001`。
- 修改噪声配置版本/窗口/步长/阈值：特征复用；噪声底、归因、画像全量进入新版本。
- 替换 `call_0002` 的 PCM 与观测：四阶段都只重算 `call_0002`，其他通话记录从上一快照复用。

新版本始终整体发布，旧版本保留在 `stages/.../versions/` 并复制归档到 `archive/`，不会增量修改。

## 追溯与自检

每个静音段和事件保存：

```text
feature_version
noise_floor_version
attribution_threshold_revision
snapshot_revision
snapshot_version
```

事件还保存来源分段 ID 和各分段快照版本。重放时：

1. 由 `snapshot_version` 加载归因快照和当时的规则配置；
2. 由 `feature_version`、`noise_floor_version` 加载上游记录；
3. 用分段证据中的能量、噪声底和过零率均值重算偏离、波动和类别；
4. 与存储的标签和偏离量比对。

链字段缺失、历史快照缺失或任一项重放不符返回 `E_LINEAGE_BROKEN`，上下文包含 `event_id`。

跨阶段自检校验：

- 四阶段 call_id 集合与 manifest 完全一致；
- 每阶段写入记录与 `completed_call_ids` 一致；
- 复用集合与重算集合无交集，并覆盖受影响集合；
- 特征 → 噪声底 → 归因 → 画像的来源校验和逐级一致；
- 事件时间轴可由归因分段重算；
- 画像统计可由事件明细重算。

任一不一致返回 `E_INCONSISTENT`。

## 业务码

| 业务码 | 场景 |
|---|---|
| `E_FRAME_MISMATCH` | 帧数或逐帧观测不一致 |
| `E_WINDOW_INVALID` | 噪声估计窗口样本不足 |
| `E_STEP_INVALID` | 滑窗步长为零 |
| `E_LABEL_INVALID` | 归因标签不在受控集合 |
| `E_SEGMENT_SPLIT` | 真值跨帧边界或静音段越过语音 |
| `E_EVENT_RANGE` | 事件越出通话时长 |
| `E_EVENT_OVERLAP` | 事件时间轴重叠 |
| `E_SNAPSHOT_ROLLBACK` | 尝试回退覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | 快照损坏、缺失或部分发布 |
| `E_LINEAGE_BROKEN` | 来源链缺失或重放不符 |
| `E_INCONSISTENT` | 跨阶段一致性失败 |

CLI 失败时返回非零退出码，并输出：

```json
{"ok":false,"code":"E_FRAME_MISMATCH","message":"...","context":{"call_id":"call_0001"}}
```

## 测试与验收

运行回归测试：

```bash
python -m unittest discover -s tests -v
```

截至 2026-11-22 的实际结果：

- `12/12` 个 unittest 通过。
- 基线 3 次通话共 20 个静音事件，标签分布符合样本设计。
- 相同样本重复完整运行，四阶段快照逐字节一致。
- 噪声底阶段第 1 个通话后强制退出码 `137`；恢复后 0 个待处理条目，四阶段快照与不间断运行逐字节一致。
- 噪声配置升级后活动版本为 features `1`、noise_floor `2`、attribution `2`、profile `2`，旧版本已归档。
- 真值修正只重算 `call_0001`，受影响帧区间为 `[220,228)`。
- PCM/观测替换只重算 `call_0002` 的四阶段后缀。
- 首个事件 `call_0001-E001` 重放为 `stationary_floor`，偏离量 `0.201443798788 dB`，与存储值一致。
- 验收触发并核对 11 个业务码。

完整机器可读结果见 `artifacts/acceptance_report.json`。

## 技术约束

- 不使用容器。
- 不使用网络或外部音频。
- 生成器、配置、观测、真值和持久化目录均可随项目本地重建。
- 快照 JSON 使用排序键和固定分隔符，不写入提交时间等非确定字段。
- 浮点结果统一保留 12 位小数后进入规范 JSON。

