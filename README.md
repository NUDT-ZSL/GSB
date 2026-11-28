# 博物馆数字展项面部动画生成器

本项目把同一段讲解的三个工程版本归一为统一音素时间轴和面部通道模型，生成协同发音平滑曲线、情绪微动、逐句同步账目，并导出展项播放器可直接读取的逐帧关键帧文件。

实现只依赖 Python 3 与 NumPy，不访问外部服务，不读取真实录音。仓库内生成器使用固定种子生成三版本 WAV、音素时间真值与面部绑定样本。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 1. 生成确定性样本
python3 main.py generate --output samples --seed 20261128

# 2. 读取 samples/manifest.json 中三个版本并运行全链路
python3 main.py pipeline \
  --samples-dir samples \
  --fps 30 \
  --threshold 0.05 \
  --output outputs/face_animation
```

也可以直接传三个版本目录：

```bash
python3 main.py pipeline \
  --version-dir samples/version_a \
  --version-dir samples/version_b \
  --version-dir samples/version_c \
  --fps 30 --threshold 0.05 \
  --output outputs/face_animation

# 或位置参数
python3 main.py samples/version_a samples/version_b samples/version_c \
  --fps 30 --threshold 0.05 --output outputs/face_animation
```

一条命令完成生成和运行：

```bash
python3 main.py demo
```

兼容入口：

```bash
python3 generate_samples.py --output samples --seed 20261128
python3 lip_sync.py --samples-dir samples --fps 30 --threshold 0.05 \
  --output outputs/face_animation
```

命令行参数：

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `--version-dir DIR` | 版本目录，重复三次 | - |
| `--versions DIR...` | 三个版本目录 | - |
| 位置参数 `DIR...` | 三个版本目录 | - |
| `--samples-dir DIR` | 从生成样本的 `manifest.json` 自动发现版本 | - |
| `--fps FLOAT` | 目标帧率 | `30` |
| `--threshold FLOAT` | 逐帧通道/同步最大允许偏差 | `0.05` |
| `--output DIR` | 输出目录 | `outputs/face_animation` |
| `--ground-truth FILE` | 独立音素真值；缺省时自动查找样本根目录的 `ground_truth.json` | 自动 |
| `--seed INT` | 仅 `generate`/`demo` 使用 | `20261128` |

## 生成样本

`generate` 固定产生以下结构：

```text
samples/
  manifest.json
  ground_truth.json
  version_a/
    annotation.json          # ARPABET，毫秒，24 fps 工程
    bindings.json            # Jaw_Open / Lip_Width / ...
    narration.wav            # 48 kHz
  version_b/
    annotation.json          # IPA，25 fps 帧序号
    bindings.json            # jawOpen / mouthWidth / ...
    narration.wav
  version_c/
    annotation.json          # PH_* 内部标签，毫秒，30 fps 工程
    bindings.json            # CTRL_JAW / CTRL_LIP_W / ...
    narration.wav
```

样本覆盖：

- `B/P/M` 闭唇音紧邻 `AA/IY/UW` 元音。
- 同一句内 40 ms 短音素与 280 ms 长音素相邻的语速骤变。
- `S04` 的 `joy` 情绪锚点与后段重音相差 680 ms，用于验证冲突裁决。
- 三版本不同的音素标签、时间单位和通道命名。

生成过程完全确定；同一种子重复运行得到相同 WAV、JSON 和哈希。

## 输入归一

内部统一表示：

- 时间基准：整数微秒，同时输出毫秒。
- 音素身份：稳定 `phoneme_id` + 规范音素符号。
- 可见口型：`SIL/PP/FF/TH/DD/SS/CH/KK/HH/RR/AA/E/I/O/U`。
- 面部通道及固定顺序：
  1. `jaw_open`
  2. `lip_width`
  3. `mouth_corner`
  4. `brow_raise`
  5. `brow_inner_up`

`annotation.json` 支持 `milliseconds`、`ms`、`frames` 等时间基准；帧序号通过版本 `fps` 转为微秒。`bindings.json` 可用列表或对象声明源通道到规范通道的绑定。若音素标签未知、缺少稳定 ID、时间重叠或 WAV 声明采样率不符，进程以非零码终止。

闭唇可见口型 `PP` 的三个唇部目标固定为：

```json
{"jaw_open": 0.0, "lip_width": 0.08, "mouth_corner": 0.0}
```

音素标签到可见口型不是一一映射。例如 ARPABET 的 `B`、IPA 的 `b`、内部标签的 `PH_B` 都归一为 `B -> PP`；`B/P/M` 同属闭唇口型。映射表中同时保留三版本原始标签、规范音素、类别、可见口型和逐通道目标。

## 平滑与表情

协同发音在内部 1 kHz 时间轴完成：

- 每个音素目标是平台段，边界使用 smoothstep 混合。
- 过渡半窗基础值为相邻音素较短时长的 20%，并限制在两侧音素各 35% 时长内。
- 音素间空隙补静默目标，因此间隔也会参与平滑宽度和闭口过程。
- 帧率重采样使用 `t = frame / fps`，帧数取 `ceil(duration * fps)`。
- 三个唇部通道在最终帧网格再做 65 ms 零相位高斯低通，避免 30 fps 下短闭唇落到两帧之间时出现帧间跳变；零相位核不移动情绪峰值时刻。

情绪调制叠加到 `brow_raise`、`brow_inner_up` 和 `mouth_corner`。每个重音以余弦包络产生局部峰值；无重音时使用情绪锚点。若情绪标签锚点与重音位置相差超过 120 ms，按固定规则 `stress_over_emotion` 裁决，表情包络跟随重音，并记录到：

- `emotion_conflicts.json`
- `difference_list.json`
- `cross_version_diff.json`

## 校验与差异

校验器从 `ground_truth.json` 的独立音素时间与目标值重建余弦插值参考曲线，逐句逐帧计算：

- 五个通道的绝对最大偏差。
- 每个音素中心帧的唇部同步偏差。
- 版本归一音素时间相对真值的起止偏差。

`--threshold` 同时约束逐帧通道偏差与音素中心同步偏差。`sync_report.json` 给出首个超阈值句子、帧、通道、音素 ID、规范音素、可见口型、三版本原始标签、前后音素和真值目标，并保留每句逐帧最大偏差数组。

`cross_version_diff.json` 对三版本归一后的通道矩阵和关键帧逐帧比较。偏差项包含版本、通道、帧、时间和双边数值；同时给出曲线、关键帧和映射的 SHA-256，供复现核对。

## 输出文件

```text
outputs/face_animation/
  face_animation.json        # 播放器固定格式，逐帧关键帧
  channel_curves.json        # 内部逐帧平滑曲线
  channel_curves.csv
  channel_curves.npz
  normalized_timeline.json   # 基线版本统一时间轴
  phoneme_mapping.json       # 音素 -> 可见口型 -> 通道目标
  sentence_ledger.json       # 逐句起止帧、目标数、最大偏差
  sync_report.json           # 独立真值校验与首个超阈值依据
  emotion_conflicts.json
  cross_version_diff.json
  difference_list.json
  summary.json
  manifest.json              # 文件清单与 SHA-256
  versions/
    A/                       # 每版本归一账目、曲线和关键帧
    B/
    C/
```

### 播放器关键帧格式

`face_animation.json` 使用固定 `museum-face-keyframes/v1`：

```json
{
  "format": "museum-face-keyframes/v1",
  "schema_version": 1,
  "fps": 30.0,
  "frame_count": 200,
  "duration_seconds": 6.666666667,
  "channel_order": [
    "jaw_open", "lip_width", "mouth_corner",
    "brow_raise", "brow_inner_up"
  ],
  "coordinate_ranges": { "...": [0.0, 1.0] },
  "keyframes": [
    {
      "frame": 0,
      "time": 0.0,
      "values": [0.0, 0.12, 0.0, 0.0, 0.0]
    }
  ],
  "curve_sha256": "..."
}
```

`keyframes` 与 `channel_curves.json` 的 `frames` 一一对应。同一帧的 `values` 数组逐元素完全相同，通道顺序由 `channel_order` 固定。播放器只需按 `fps` 读取 `values`。

## 错误码

错误 JSON 写入请求输出目录的 `error.json`，同时以结构化 JSON 输出到 stderr；进程返回码为 `2`，不会提交新的关键帧/曲线片段。

| 错误码 | 含义 |
| --- | --- |
| `E_INPUT_MISSING` | 标注、绑定、音频或清单文件不存在 |
| `E_JSON_INVALID` | JSON 语法或根结构错误 |
| `E_MISSING_PHONEME` | 音素 ID、标签、真值音素、情绪/重音引用缺失 |
| `E_EXTRA_PHONEME` | 某版本出现真值之外的音素 |
| `E_UNKNOWN_PHONEME` | 音素标签无法归一 |
| `E_DUPLICATE_PHONEME` | 同一版本内音素 ID 重复 |
| `E_DUPLICATE_SENTENCE` | 同一版本或真值内句子 ID 重复 |
| `E_SENTENCE_IDENTITY` | 某版本的句子 ID 与真值不一致 |
| `E_TIME_OVERLAP` | 同句或跨句音素时间重叠 |
| `E_INVALID_INTERVAL` | 结束时间不晚于开始时间 |
| `E_MISSING_TIME` | 缺少开始/结束时间 |
| `E_INVALID_TIME` | 时间为负 |
| `E_INVALID_NUMBER` | 数值字段不可解析 |
| `E_TIME_BASE` | 时间基准不支持或缺失 |
| `E_SAMPLE_RATE_MISMATCH` | WAV 实际采样率、声明采样率、fps 或目标帧率不一致 |
| `E_AUDIO_INVALID` | WAV 无法读取 |
| `E_BINDING` | 面部通道绑定缺失、重复或结构错误 |
| `E_UNKNOWN_CHANNEL` | 通道名无法归一 |
| `E_GROUND_TRUTH` | 独立真值结构或目标值错误 |
| `E_INVALID_THRESHOLD` | 阈值非有限值或为负 |
| `E_VERSION_COUNT` | 版本数量不是三个或版本 ID 重复 |
| `E_ARGUMENT_CONFLICT` | 同时使用互斥的版本输入方式 |
| `E_OUTPUT` | 输出文件未成功生成 |
| `E_INTERNAL` | 未预期的内部异常，附异常类型 |

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖默认运行、固定帧一致性、闭唇零开合、跨版本哈希一致、情绪冲突、低阈值报告、时间重叠、采样率不符和缺失音素。
