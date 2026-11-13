# 配音长录音切片与响度归一化工具

把配音长录音按剧本时间点切成短语音，自动加淡入淡出并将各片段响度
（RMS）归一化到统一目标，方便导入游戏引擎。**仅依赖 Python 3.10+
标准库**（`wave` / `array` / `json` / `math` / `argparse`），无需第三方包。

## 目录结构

```
project42/
├── cli.py                  # 命令行入口（支持 python -I 隔离模式）
├── audio_cutter/
│   ├── __init__.py
│   ├── __main__.py         # python -m audio_cutter 入口
│   ├── cutter.py           # 核心：WAV 校验、切片、淡变、RMS 归一化、报告
│   ├── samplegen.py        # 确定性样本与剪辑点构造（无随机数）
│   └── demo.py             # 一键复跑 + 34 项自动验收
└── demo_workspace/         # 运行后生成（样本、剪辑点、输出、报告）
```

## 一键复现（推荐演示入口）

```powershell
# 在项目根目录
python -m audio_cutter.demo
```

该命令会清空并重建 `demo_workspace/`，确定性生成 44.1kHz 单声道、
48kHz 立体声样本及各类剪辑点，然后通过子进程以真实命令行方式跑完
全部 15 条验收项，打印 `34/34 通过`。重复运行 WAV 文件 MD5 完全一致。

## 命令行用法

```powershell
python -I cli.py --input <输入WAV> --clips <剪辑点JSON> --output-dir <输出目录> `
                 [--fade 淡变秒] [--target-dbfs 目标响度] [--report-name 报告名]
```

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `--input` | 是 | — | 16 位 PCM WAV，单/双声道，44.1k/48kHz |
| `--clips` | 是 | — | 剪辑点 JSON（时间单位：秒） |
| `--output-dir` | 是 | — | 片段输出目录，缺失自动创建 |
| `--fade` | 否 | `0.01` | 淡入/淡出时长（秒），可设 0 |
| `--target-dbfs` | 否 | `-20` | 目标 RMS 响度（dBFS） |
| `--report-name` | 否 | `report.json` | 报告文件名 |

缺必填参数时 argparse 自动打印用法并以 **退出码 2** 退出。

### 业务操作样例

```powershell
# 1) 生成确定性样本（也可在自己的代码里调用 samplegen.build_all）
python -c "from audio_cutter.samplegen import build_all; build_all('demo_workspace')"

# 2) 48kHz 立体声，默认 0.01s 淡变、-20dBFS 目标
python -I cli.py `
  --input demo_workspace/samples/source_48000_stereo.wav `
  --clips demo_workspace/clips/valid_clips.json `
  --output-dir demo_workspace/out_stereo

# 3) 44.1kHz 单声道，自定义 0.02s 淡变、-18dBFS 目标
python -I cli.py `
  --input demo_workspace/samples/source_44100_mono.wav `
  --clips demo_workspace/clips/valid_clips.json `
  --output-dir demo_workspace/out_mono `
  --fade 0.02 --target-dbfs -18

# 4) 触发增益下限 0.1 的强音场景（目标压到 -24dBFS）
python -I cli.py `
  --input demo_workspace/samples/source_48000_stereo.wav `
  --clips demo_workspace/clips/valid_clips.json `
  --output-dir demo_workspace/out_low_gain --target-dbfs -24
```

## 剪辑点 JSON 格式

```json
{
  "clips": [
    {"id": "c001", "start": 0.20, "end": 1.00},
    {"id": "c002", "start": 1.10, "end": 1.90}
  ]
}
```

也接受裸列表 `[{"id": ..., "start": ..., "end": ...}]`。`start`/`end`
单位为秒，要求 `0 <= start < end <= 音频时长`。片段标识即输出文件名
（`<id>.wav`），禁止路径分隔符与 `..`。

## 退出码约定

| 码 | 含义 |
|----|------|
| 0 | 全部片段成功 |
| 2 | 参数缺失/非法（argparse） |
| 3 | 输入校验失败（WAV 格式或剪辑点越界/倒置），**不产出任何 WAV** |
| 4 | 处理中 I/O 故障中断，已产出文件保留，报告记录中断原因 |
| 5 | 流程跑完但存在失败片段（失败原因写入报告） |

## 关键处理规则

- **格式保持**：输出 WAV 与输入同采样率、同声道、同 16 位 PCM，不做转换。
- **淡入淡出**：线性淡变，默认首尾各 0.01s；请求时长超过片段一半时
  自动缩短到一半，并在该片段 `notes` 中给出提示。
- **RMS 归一化**：按淡变后整体 RMS 计算增益，增益夹在 `[0.1, 10]`；
  触限会在 `gain_clamped` 与 `notes` 中记录。立体声左右声道共用同一
  增益，维持声道间相对电平。
- **失败隔离**：单个片段裁切失败（如取整后为空）不影响其他片段，
  失败项进入报告 `failures`，并按 `id` 升序排列。
- **确定性**：信号由固定正弦/谐波/方波与定点取整规则生成，报告不含
  时间戳；同参数重复运行，WAV 字节 MD5 完全一致。

## 报告 report.json 字段

- 顶层：`status`、`input_summary`、`success_count`、`failure_count`、
  `successes`、`failures`、`interrupted_reason`。
- 成功条目含：`id`、`output_file`、`overwritten`、采样率/声道/帧数、
  原始与处理后时长、原始与处理后 RMS（整体及各声道）、`gain`、
  `gain_clamped`、`fade_applied`、实际淡入淡出时长与帧数、`notes`。
- 失败条目含：`id`、`reason`。
