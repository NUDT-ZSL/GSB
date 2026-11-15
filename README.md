# 离线多轨音频转码引擎

本项目把若干条本地 PCM WAV 音轨按毫秒偏移放到统一时间轴，重采样到目标采样率，
完成单声道/立体声到目标声道布局的混音、逐轨增益、源时间静音、分段 RMS/峰值报告，
并在每个阶段原子写入可恢复状态。全部处理离线完成，不依赖网络服务或容器。

## 环境准备

- Python 3.10 或更高版本
- NumPy
- 本机 `ffmpeg` 与 `ffprobe`，并加入 `PATH`

Windows PowerShell 示例：

```powershell
py -3 -m pip install -r requirements.txt
ffmpeg -version
ffprobe -version
```

也可以不修改全局 `PATH`，通过 `--ffmpeg` 和 `--ffprobe` 指定可执行文件：

```powershell
python transcoder.py demo\manifest.json `
  --ffmpeg C:\tools\ffmpeg\bin\ffmpeg.exe `
  --ffprobe C:\tools\ffmpeg\bin\ffprobe.exe
```

程序启动时会实际执行两个工具的 `-version`；缺失或启动失败返回 JSON 错误。

## 快速开始

生成完全确定、无需网络下载的本地样本：

```powershell
python tools\generate_samples.py --output-dir demo
python transcoder.py demo\manifest.json
```

PowerShell 一键脚本：

```powershell
.\tools\run_demo.ps1
```

成功时 stdout 只有一行 JSON，例如：

```json
{"ok":true,"status":"completed","snapshot_id":"...","output":"...","report":"...","state":"...","timeline":{"sample_rate":48000,"channels":2,"channel_layout":["left","right"],"total_frames":108000,"total_duration_ms":2250.0},"report_summary":{"window_count":23,"window_frames":4800,"overall_rms":0.1469980906432876,"overall_peak":0.699999988079071},"elapsed_ms":197.8}
```

退出码为 `0`。所有失败也输出 JSON，并把错误业务码和约定退出码放在 JSON 中。

## 清单格式

```json
{
  "tracks": [
    {
      "file": "voice_48k_mono_16.wav",
      "sample_rate": 48000,
      "bit_depth": 16,
      "offset_ms": 0,
      "gain": 1.0,
      "silences": [
        {"start_ms": 400, "end_ms": 600}
      ]
    },
    {
      "file": "music_44k1_stereo_24.wav",
      "sample_rate": 44100,
      "bit_depth": 24,
      "offset_ms": 300,
      "gain": 0.8,
      "silences": [],
      "channel_map": [0, 1]
    }
  ],
  "output": {
    "file": "mix.wav",
    "sample_rate": 48000,
    "channels": 2,
    "channel_layout": ["left", "right"],
    "bit_depth": 16,
    "sample_format": "pcm_s16le",
    "statistics_window_ms": 100,
    "duration_ms": 3000,
    "max_workers": 3,
    "memory_limit_mb": 256,
    "peak_policy": "clip"
  },
  "report_file": "mix.report.json",
  "state_file": "mix.state.json",
  "work_dir": ".mix-work"
}
```

轨道字段：

- `file`: 相对于清单目录的 WAV 路径；绝对路径也可。
- `sample_rate`, `bit_depth`: 必填，必须与 `ffprobe` 探测到的实际 WAV 参数一致。
- `offset_ms`: 必填，非负整数毫秒。换算到目标采样点时采用 half-up 舍入，
  相对真实毫秒位置的误差不超过 `0.5` 个成品采样点。
- `gain`: 可选，默认 `1.0`；必须是有限浮点数，可为负数。
- `silences`: 可选，按该轨**源时间**给出的静音区间；起止可为非负整数或小数毫秒。
- `channel_map`: 可选，长度必须等于输出声道数。数组第 `j` 项表示输出第 `j` 声道
  取自哪个源声道；`-1` 表示不取源声道。

同一 WAV 可被多条轨道引用，只要每条轨道的偏移、增益、静音或映射可以不同。

也可把输出规格放在独立 JSON 文件中，通过 `--output-spec spec.json` 合并。
独立规格中的相对路径相对于该规格文件所在目录解析。

输出字段：

- `file`: 必填，输出 `.wav` 路径；也可由 CLI `--output` 覆盖。
- `sample_rate`: 必填，正整数。
- `channels` / `channel_layout`: 至少给出一个。支持 `mono`、`stereo`、`5.1`、
  `7.1` 等名称或声道名数组；省略时默认立体声。
- `bit_depth`: 默认 `16`，整数输出支持 `8/16/24/32`。
- `sample_format`: 可选；默认按位深选 `pcm_u8/pcm_s16le/pcm_s24le/pcm_s32le`。
  32 位浮点可显式写 `pcm_f32le`。
- `statistics_window_ms`: 默认 `1000`，正数。
- `duration_ms`: 可选。指定时成品严格为该时长，超出的轨道截断，不足处零填充；
  省略时成品终点为所有轨道 `offset + 实际解码后的轨道长度` 的最大值。
- `max_workers`: 默认 `4`，范围 `1..64`。
- `memory_limit_mb`: 默认 `512`，用于限制并行解码时的临时浮点缓冲区总量。
- `peak_policy`: `clip`（默认）或 `scale`。
- `report_file`, `state_file`, `work_dir`: 可放在根节点或 `output` 内。

默认路径为：

```text
mix.wav
mix.wav.report.json
mix.wav.state.json
./.mix.work/
```

CLI 的 `--output`、`--report`、`--state`、`--work-dir` 分别覆盖上述路径。

## 时间轴与混音语义

1. 每条输入先用 `ffprobe` 校验 WAV、PCM、实际采样率/位深/声道数。
2. 用 `ffmpeg` 并行解码并重采样到成品采样率，但保留源声道布局。
3. 轨道源时间 `0` 对齐到成品采样点
   `round_half_up(offset_ms * output_sample_rate / 1000)`；偏移之前零填充。
4. 声道映射后应用线性增益，再在成品时间轴上按源时间换算的采样区间精确置零。
5. 按清单顺序累加所有轨道到 `float32` 时间轴。完成顺序不影响结果。
6. 默认 `clip` 将所有落在 `[-1, 1]` 之外的样本夹紧；`scale` 在峰值大于 1 时
   对整个成品乘 `1 / pre_limit_peak`，否则不变。两种策略的实际参数写入报告。

静音区间按左闭右开 `[start_ms, end_ms)` 处理，起止毫秒换算到目标采样点时同样使用
half-up 舍入。

默认声道规则：

- 单声道 -> 单声道：原样。
- 立体声 -> 单声道：`(L + R) / 2`。
- 单声道 -> 立体声：复制到左右声道。
- 立体声 -> 立体声：`L -> left`、`R -> right`。
- 多声道目标：默认将单声道送入 `center`（无该声道时送第一个声道），
  立体声送入 `left/right`。其他路由使用 `channel_map` 精确定义。

静音在时间轴置零而不是依赖文件裁剪，因此静音区间前后的成品样本不会被淡入淡出；
区间内严格为数值 `0`。输入并非 PCM WAV、声明与实际参数不一致、静音区间超出源时长
等情况会返回业务错误而不创建成品。

## 响度报告

`report.json` 按固定采样点窗口统计：

```json
{
  "sample_rate": 48000,
  "channels": 2,
  "window_frames": 4800,
  "window_duration_ms": 100.0,
  "total_frames": 108000,
  "total_duration_ms": 2250.0,
  "window_count": 23,
  "overall": {"rms": 0.1469, "peak": 0.7},
  "limiter": {
    "policy": "clip",
    "pre_limit_peak": 0.7,
    "applied_scale": 1.0,
    "post_limit_peak": 0.7
  },
  "windows": [
    {
      "index": 0,
      "start_sample": 0,
      "end_sample": 4800,
      "duration_samples": 4800,
      "start_ms": 0.0,
      "end_ms": 100.0,
      "empty": false,
      "silent": false,
      "rms": 0.1555,
      "peak": 0.2200
    }
  ]
}
```

- 窗口从成品采样点 `0` 开始，`window_frames = round_half_up(window_ms * sr / 1000)`。
- 时间字段严格由 `start_sample/sample_rate` 和 `end_sample/sample_rate` 计算。
- 最后窗口取剩余采样点；没有有源音频但仍在成品时长内的窗口，`rms` 和 `peak`
  明确为 `0.0`。
- `rms`、`peak` 均使用归一化满量程振幅，统计的是限幅/缩放后的成品。

## 状态与恢复

状态文件 JSON 原子更新，包含：

- `snapshot_id`: 输入内容 SHA-256、轨道参数和影响音频的成品规格的规范化摘要。
- `stage`: `prepared/loading/mixing/statistics/encoding/complete/interrupted/failed`。
- `completed_tracks`: 已完成的轨道及其缓存文件、帧数、偏移和目标声道数。
- `recovery`: `resume/restart`、原因和复用轨道数。
- `interrupted_stage`: 可处理信号中断时停止在哪个阶段。

再次运行时：

- 快照相同且缓存完整：复用已完成轨道，从剩余阶段继续。
- 快照变化：从 `loading` 重做，不复用旧轨道缓存。
- 状态为 `complete` 且成品、报告仍存在：直接返回 `recovered_complete`。
- `--force-restart` 强制忽略旧状态和旧缓存。

`Ctrl+C`、`SIGINT`、`SIGTERM`（Windows 可用时含 `SIGBREAK`）会设置取消标志，
终止正在运行的 FFmpeg 子进程，在阶段边界写入 `interrupted` 状态并以退出码 `130`
返回 JSON。强制结束（Windows `taskkill /PID <pid> /F`、POSIX `SIGKILL`）不承诺
退出报告，但此前原子写入的阶段、已完成轨道和轨道缓存仍可复用。

成品先写同目录隐藏 `.wav.part` 临时文件，编码成功后再 `os.replace` 原子替换；
报告和状态文件同样使用临时文件原子替换。

## 并发与内存

解码使用大小固定的 `ThreadPoolExecutor`，并发数来自 `max_workers`。每个工作线程
只在处理一个固定帧块时获取内存预算；原始解码块和结果缓存落在工作目录，混音与统计
也分块读写内存映射文件，因此不会同时把全部轨道装入 RAM。任一线程失败会设置停止标志、
取消排队任务并终止其他 FFmpeg 子进程，最终按最小轨道索引返回确定性错误。

## 错误码与退出码

错误 JSON 结构：

```json
{
  "ok": false,
  "error": {
    "code": "INPUT_MISMATCH",
    "business_code": 1005,
    "exit_code": 3,
    "category": "轨道声明与 WAV 实际参数不一致",
    "message": "轨道声明位深与实际 WAV 不一致",
    "details": {"track_index": 0, "declared": 16, "actual": 24}
  }
}
```

| code | business_code | exit | 含义 |
|---|---:|---:|---|
| `INVALID_ARGUMENTS` | 1001 | 2 | CLI 参数错误 |
| `INVALID_JSON` | 1002 | 3 | 清单 JSON 无效 |
| `INVALID_MANIFEST` | 1003 | 3 | 轨道字段/静音区间无效 |
| `INPUT_NOT_FOUND` | 1004 | 3 | 输入文件不存在或不可读 |
| `INPUT_MISMATCH` | 1005 | 3 | 声明采样率/位深与实际不符 |
| `UNSUPPORTED_FORMAT` | 1006 | 3 | 非 PCM WAV 或不支持的声道 |
| `INVALID_OUTPUT_SPEC` | 1007 | 3 | 输出规格无效 |
| `DEPENDENCY_MISSING` | 2001 | 4 | ffmpeg/ffprobe 不可用 |
| `DECODE_FAILED` | 2002 | 5 | 解码/探测失败 |
| `RESOURCE_LIMIT` | 2003 | 6 | 内存预算无法满足单次块 |
| `OUTPUT_WRITE_FAILED` | 2004 | 7 | 成品、报告或状态写入失败 |
| `INTERRUPTED` | 3001 | 130 | 收到可处理终止信号 |
| `INTERNAL_ERROR` | 9000 | 1 | 未预期内部错误 |

## 测试

```powershell
py -3 -m unittest discover -s tests -v
```

测试脚本运行时生成本地 WAV，不读取仓库内二进制音频附件。覆盖输入校验、44.1 kHz
到 48 kHz 冲激对齐、单/立体声映射、增益、精确静音、统计窗口、限幅/缩放、
并发确定性、快照重做、`SIGINT` 中断和强制终止后的缓存复用。

实际执行结果见 [VALIDATION.md](VALIDATION.md)。
