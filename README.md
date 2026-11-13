# 播客后期命令行工具 (podcast_editor)

按 JSON 编辑脚本对**单声道 16bit PCM WAV** 做片段截取、顺序拼接、首尾线性
淡入淡出与整体统一音量调整，导出 WAV 与 JSON 处理报告。仅依赖 Python 3.10+
标准库（`wave` / `json` / `array` / `math`），全程离线，输出确定性可复现。

## 1. 环境要求

- Python 3.10 及以上（开发与验证版本：Python 3.10.20，Windows 11）
- 无第三方依赖、无容器、无网络访问

## 2. 文件说明

| 文件 | 作用 |
| --- | --- |
| `podcast_editor.py` | 命令行主工具 |
| `make_samples.py` | 用标准库 `wave` 确定性生成测试样本 WAV |
| `run_tests.py` | 15 条验收点的端到端自动化测试 |
| `demo.py` | 业务操作样例（含失败片段取舍演示） |

生成目录（已加入 `.gitignore`）：`samples/`（输入样本）、`tests_work/`（测试产物）、`outputs/`（演示产物）。

## 3. 本地启动（可复现步骤）

```powershell
# 1) 生成确定性样本 (samples/*.wav，重复运行逐字节一致)
python -I make_samples.py

# 2) 执行全部 15 条验收测试
python -I run_tests.py

# 3) 运行业务操作样例
python -I demo.py
```

`-I` 为隔离模式（忽略当前目录与环境变量中的模块路径），防止同名 `json.py`
之类的植入文件被解释器加载。Linux/macPOSIX 下同样适用。

## 4. 命令行用法

```
python podcast_editor.py <输入WAV> <编辑脚本JSON> <输出WAV> <报告JSON>
```

- 四个参数均为必填；缺参时打印用法到 stderr、以退出码 **2** 退出，**不创建任何文件**。
- 输入 WAV 不可读 / 格式不兼容、脚本不是合法 JSON / 缺必要字段：退出码 **1**，
  错误信息可读且包含具体原因，**不写出任何输出文件**。
- 片段级错误（越界、零时长、起止颠倒、淡变超标等）只拒绝对应片段，
  其他合法片段照常导出；工具整体仍以 0 退出，拒绝原因写入报告。

## 5. 编辑脚本格式

```json
{
  "volume": 130,
  "segments": [
    {
      "start_ms": 0,
      "end_ms": 800,
      "fade_in_ms": 100,
      "fade_out_ms": null
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `volume` | 整体音量档位，百分比整数，允许区间 `[0, 200]`，默认 `100`。非法值（如 `500`）不致命：回退默认档位导出，并在报告中标注非法原因。 |
| `segments[].start_ms` / `end_ms` | 片段起止时间，毫秒，相对输入音频起点；必须 `end_ms > start_ms` 且落在音频范围内。 |
| `fade_in_ms` / `fade_out_ms` | 淡入/淡出时长（毫秒）；`null` 表示该端不做淡变。增益严格限定 `[0, 1]` 线性变化，不会产生负增益或放大。 |

### 片段拒绝规则（均只影响该片段）

1. 字段缺失或类型非法；
2. `start_ms` / `end_ms` 为负，或 `end_ms <= start_ms`（起止颠倒/零时长）；
3. 换算成采样帧后超出输入音频总帧数（越界），或不足 1 帧；
4. 淡入帧数 + 淡出帧数 **>= 片段帧数**（增益区间会重叠产生抵消/负增益，拒绝而非钳制硬导）。

### 时间轴换算

毫秒 → 帧数采用 `round(ms × 采样率 / 1000)`（银行家舍入，确定无平台差异）。
片段按脚本顺序逐帧拼接，片段之间不插静音、不重叠；输出 WAV 的采样率、
声道数、位宽与输入一致（16kHz/单声道/16bit 输入 → 同样规格输出）。

### 音量缩放与钳制

拼接完成后对整体输出统一乘以 `volume / 100`，再做边界钳制：
结果大于 `32767` 取 `32767`，小于 `-32768` 取 `-32768`（饱和，不回绕）。

## 6. 报告格式（JSON）

- `accepted_segments` / `rejected_segments` 均按脚本片段序号（从 0 开始）升序；
- `total_frames` 与输出 WAV 头 `getnframes()` 严格相等；
- `volume.valid=false` 时 `applied` 为实际采用的默认档位，`reason` 说明非法原因；
- 报告以固定键序、LF 换行写出，不含时间戳等非确定字段，同一输入重复运行逐字节一致。

## 7. 业务操作样例

`demo.py` 的脚本从 4 秒素材中选取三段口播（分别带淡入、双端淡变、淡出），
整体音量 130%，并故意混入一个越界片段与一个淡变超标片段。实际运行结果：

- 3 个合法片段导出，输出 2.400s / 38400 帧；
- 2 个非法片段进入 `rejected_segments`，分别注明越界帧区间与淡变超标帧数；
- 产物：`outputs/demo_out.wav`、`outputs/demo_report.json`、`outputs/demo_script.json`。

手工单条调用示例：

```powershell
python -I podcast_editor.py `
  samples/source_16k_mono.wav `
  outputs/demo_script.json `
  outputs/demo_out.wav `
  outputs/demo_report.json
```

## 8. 验收结果

`python -I run_tests.py` 实测 **15/15 全部通过**（覆盖任务书全部验收点，
含逐字节可复现哈希比对、淡变增益逐样本核对、钳制峰值 32767/-32768 等）。
