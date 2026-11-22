# 会议同传后处理控制台

一个从零构建、完全本地运行、无容器依赖的会议同传后处理专业演示应用。项目使用固定种子合成 16 kHz 单声道会议语音，围绕**样本索引唯一时间基准**实现逐帧真值、滞回分段、确定性说话人掩码、注册样本重识别、三版本配置互操作、可审计迁移和版本化快照防护。

运行依赖仅为 Python 3.10+ 与 NumPy 1.26；本地 Web 界面只使用 Python 标准库，不访问网络、外部模型或外部数据。

## 快速开始

```bash
cd /home/zsl/feedback/uid2d1b8ac8-default-259
python3 -m meeting_post demo
python3 -m meeting_post test
python3 -m meeting_post serve --port 8765 --open
```

打开 `http://127.0.0.1:8765/`。仪表盘可交互完成：

- v1 / v2 / v3 配置切换，并强制失效旧结果后重算；
- 严格迁移、显式有损迁移及逐字段补全/丢弃审计；
- 三个快照项的完整替换、回退探针与损坏探针；
- 波形、真值段、分离段、逐段身份、置信区间和毫秒/帧/样本对照；
- 当前配置三视图、快照版本/校验和、最近错误码和操作事件；
- 跨模块一致性自检。

## 验证命令

```bash
# 生成全部样例、执行三版本互操作与异常验收，写出 data/demo/run_log.txt
python3 -m meeting_post demo

# 只输出验收布尔结果
python3 -m meeting_post selftest

# 运行 42 项 unittest
python3 -m meeting_post test

# 单独迁移一个配置；严格模式遇到不可保真字段返回 E_SEMANTIC_LOSS
python3 -m meeting_post migrate \
  --input configs/meeting.v3-stress.json \
  --target v1 \
  --output data/runtime/stress.v1.txt

# 允许显式丢弃/展平后降级
python3 -m meeting_post migrate \
  --input configs/meeting.v3-stress.json \
  --target v1 --lossy \
  --output data/runtime/stress.lossy.v1.txt
```

## 目录

```text
meeting_post/
  config.py       # v1/v2/v3 解析、规范语义、双向迁移与审计
  synthesis.py    # 固定种子语音合成、混合 PCM、逐帧真值与注册片段
  features.py     # 短时能量、过零率、频带嵌入与注册声纹统计
  timebase.py     # 样本索引权威层、毫秒 half-up 0.001ms 交付与反算
  segmentation.py # 双门限滞回 VAD、确定性掩码与段重建
  reid.py         # 注册样本相似度、身份指派、置信区间与真值核对
  snapshots.py    # 分段/分离/重识别快照的版本、校验和、回退保护
  pipeline.py     # 端到端处理、快照一致性检查与结果摘要
  selfcheck.py    # 跨模块覆盖、身份一一对应、时间基准自检
  demo.py         # 可重复样例、互操作验收与迁移/快照实验
  app_state.py    # 仪表盘状态机、结果失效与操作审计
  webapp.py       # 本地 HTTP 仪表盘
  cli.py          # demo / selftest / test / migrate / serve
configs/          # 三版本配置样例与有损迁移压力样例
tests/            # 42 项标准库 unittest
data/demo/        # 生成的 PCM/WAV、真值、特征、掩码、段、身份与运行记录
data/runtime/     # 持久化快照及快照实验目录
docs/             # 设计与验证说明
```

## 三版本配置

v1 是位置列行式：

```text
v1|16000|4000|3|1.8
```

字段依次为 `sample_rate`、`duration_ms`、`max_speakers`、`threshold_multiplier`。缺失的帧长、帧移、归一化、变体与重识别阈值由规范默认值补全并记录来源。

v2 是带字段名的行式：

```text
version|v2
sample_rate|16000
duration_ms|4000
max_speakers|3
threshold_multiplier|1.8
frame_ms|20
hop_ms|20
normalization|rms
variant|balanced
reid_threshold|0.55
```

v3 是结构化 JSON，增加逐说话人增益、掩码模式和精度声明：

```json
{
  "format": "meeting-segment-config",
  "version": "v3",
  "session": {"sample_rate": 16000, "duration_ms": 4000, "max_speakers": 3},
  "segmentation": {
    "frame_ms": 20,
    "hop_ms": 20,
    "threshold_multiplier": 1.8,
    "normalization": "rms",
    "variant": "balanced"
  },
  "separation": {
    "speaker_gains": {"1": 1, "2": 1, "3": 1},
    "mask_mode": "binary",
    "precision": "float64"
  },
  "reid": {"threshold": 0.55}
}
```

三种格式解析后都归一为 `MeetingConfig`，并以规范语义 SHA-256 逐字段比较。`frame_ms` 固定为真值帧长 20 ms；v2/v3 可使用更小的 `hop_ms`，重叠分析帧仍与逐帧真值保持同一数量与样本边界。

## 工件与确定性

`python3 -m meeting_post demo` 在 `data/demo/` 生成：

- `mixed.pcm`：原始 little-endian int16 PCM；
- `session.wav`：16 kHz 单声道 WAV；
- `truth.json`：逐说话人逐帧真值（`frame_index`、`speaker_id`、`is_speech`、`sample_start`、`sample_end`）；
- `features.json`：能量、过零率、谱质心与频带能量；
- `masks.json`、`segments.json`、`identities.json`；
- `registrations/speaker_XX.{wav,pcm}`；
- `demo_report.json`、`run_log.txt`、`sha256sums.txt`。

同一 Python/NumPy 环境、同一种子、同一规范配置下，PCM、特征、掩码与段列表逐字节一致。`demo_report.json` 记录实际 SHA-256 和所有验收结果。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_VERSION_UNSUPPORTED` | 未知配置版本，附版本与行号 |
| `E_FORMAT_CORRUPT` | 字段数、字段缺失、JSON 或数值损坏，附行号与字段名 |
| `E_SEMANTIC_LOSS` | 降级目标无法保真，附字段清单 |
| `E_TIME_BASE_MISMATCH` | 毫秒/帧/样本无法唯一互反，或段跨零帧，附段与帧区间 |
| `E_SNAPSHOT_ROLLBACK` | 尝试用旧版本覆盖新快照 |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON、身份或校验和损坏，附项与版本 |
| `E_SPEAKER_UNKNOWN` | 身份标签越出会话声明集合 |
| `E_INCONSISTENT` | 跨模块一致性自检失败 |
| `E_RESULT_STALE` | 快照或配置已变化，旧派生结果不得沿用 |
| `E_CONFIG_INVALID` | 规范语义不合法（如帧长不是 20 ms） |

## 设计边界

- 合成会议采用三位说话人轮流发言且保留帧级间隙，以明确逐帧真值归属；分离算法本身基于注册声纹与帧频带特征，不读取真值。
- 掩码输出为确定性二值或加权掩码；示例配置为 `binary`，压力配置用于验证有损降级审计。
- 置信区间是基于逐帧相似度的确定性 95% 正态区间，不声称是校准概率。
- 不包含容器、数据库、网络服务或第三方声学模型；快照使用原子文件替换。

更完整的机制说明见 `docs/DESIGN.md`，实际运行记录见 `docs/VALIDATION.md`。
