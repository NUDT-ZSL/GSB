# 16 位 PCM WAV 时间线拼接工具（纯 Python 标准库）

按毫秒时间线把多个 16 位 PCM WAV 片段拼接/混音为一条音轨，并在指定时间点插入命名标记。
仅依赖 Python 3.10+ 标准库（`wave`、`array`、`struct`、`json`、`math`、`argparse` 等），无需安装第三方库，不使用容器。

## 目录文件

| 文件 | 作用 |
| --- | --- |
| `wav_editor.py` | 主程序：清单校验、重采样、混音限幅、标记校验、三份产物输出 |
| `make_samples.py` | 生成确定性测试 WAV（正弦波，无随机数）与 11 份编辑清单 |
| `run_tests.py` | 覆盖 10 条验收项的自动化测试（45 个断言），独立用 `wave` 交叉校验 |
| `sample_data/` | 运行 `make_samples.py` 后生成：`wav/` 样本与 `manifests/` 清单 |
| `test_output/` | 测试产物目录 |

## 本地启动（可复现）

```powershell
# 1) 生成样本与清单（确定性，可重复运行）
python -I make_samples.py

# 2) 运行全部验收测试
python -I run_tests.py

# 3) 业务操作：按清单生成音轨
python -I wav_editor.py sample_data/manifests/manifest_01_basic.json demo_run
```

> Windows 控制台若出现中文乱码，先执行 `chcp 65001` 或设置环境变量 `PYTHONUTF8=1`，不影响 JSON/报告文件内容（文件始终为 UTF-8）。
> `-I` 为隔离模式，避免当前目录下被植入的同名模块（如 `json.py`）干扰解释器。

## 编辑清单格式

```json
{
  "output": { "wav": "output.wav", "markers": "markers.json", "report": "report.json" },
  "clips": [
    { "path": "../wav/a.wav", "start_ms": 0, "duration_ms": 200, "gain_db": 0 },
    { "path": "../wav/b.wav" }
  ],
  "markers": [
    { "name": "verse-in", "time_ms": 100 }
  ]
}
```

字段规则：

- `clips[].path`：WAV 路径，相对路径基于**清单文件所在目录**解析。
- `start_ms`：片段在时间线上的起始毫秒，省略时紧接上一片段末尾；必须为非负有限数。
- `duration_ms`：取用时长毫秒，省略时取源文件全长；为负/非有限数整单拒绝。
- `gain_db`：增益分贝，允许范围 **[-60, +12]**，必须为有限数值（文本、NaN 均拒绝），缺省 0。
- 采样率统一到**首片段采样率**（线性插值重采样）；支持单/双声道互转（立体声转单声道取左右均值）。
- 重叠区按浮点累加各片段采样值，最终统一限幅到 `[-32768, 32767]`，**硬限幅不回绕**。
- `markers[].time_ms` 为负或超出输出总时长的标记**逐条拒绝**，其余保留；拒绝原因写入报告。
- 空片段清单（或合计 0 帧）：生成 0 帧 WAV（无参考采样率时兜底 44100Hz/单声道），标记 JSON 为空，所有标记在报告中记为越界。
- 任何整单校验失败：退出码非 0、错误逐条打印到 stderr 并含完整字段路径（如 `clips[0].gain_db`），**输出目录不新增任何文件**。
- 输出采用“临时文件 + 原子替换”，同名旧文件会被覆盖。

## 输出产物

1. `output.wav`：16 位 PCM WAV，采样率与首片段一致，可被 Python `wave` 等标准库读取。
2. `markers.json`：仅含有效标记（名称、毫秒时间、对应帧号）及音轨头信息。
3. `report.json`：摘要报告，含总时长毫秒、采样率、声道数、总帧数、有效/被拒标记数、峰值、每片段放置信息与逐条拒绝原因。

## 10 条验收项对照

| 验收 | 测试函数 / 清单 | 结果 |
| --- | --- | --- |
| 1 三文件输出且文件名符合清单 | `test_01_basic_and_timeline` / manifest_01 | 3 文件齐全 |
| 2 时长截取 + 无起始首尾相接（200ms+300ms=500ms，22050 帧） | 同上 | 通过 |
| 3 44100/22050 统一到 44100Hz，帧数 26460 | `test_03_resample` / manifest_02 | 通过 |
| 4 满幅片段重叠 50ms，全部采样在界内且双向限幅无回绕 | `test_04_overlap_clamp` / manifest_03 | min=-32768/max=32767 |
| 5 空片段 0 帧、标记 JSON 为空、报告标记越界 | `test_05_empty` / manifest_04 | 通过 |
| 6 负/越界标记逐条拒绝、有效标记保留、原因逐条列出 | `test_06_marker_bounds` / manifest_05 | 通过 |
| 7 文件缺失 / 8 位非 16 位 PCM：非零退出、零输出、终端显示路径 | `test_07_bad_inputs` / manifest_06、07 | 通过 |
| 8 增益 +20dB 拒绝零输出；+6dB 输出峰值 ≤32767 可读 | `test_08_gain` / manifest_08、09 | 峰值 32688 |
| 9 同名旧文件被覆盖，报告数值与实读 WAV 完全一致 | `test_09_overwrite_report` / manifest_10 | 通过 |
| 10 start_ms=-10 与 gain_db 文本同时报错，列出两个字段、零输出 | `test_10_field_errors` / manifest_11 | 通过 |

最近一次完整运行结果：**PASS 45 / FAIL 0**。
