# Podcast Segment Concatenator

将清单中的多段单声道 WAV 按顺序拼接成一条节目，插入静音、做峰值归一化，并输出 `output.wav` 与 JSON 审听报告。仅使用 Python 3.10+ 标准库，在 Windows 上确定性运行。

## 环境

```powershell
python --version    # 需要 >= 3.10，本机验证为 Python 3.10.20
```

无第三方依赖，无需 `pip install`，不使用 Docker 或任何容器。

## 快速开始（可复现演示）

在仓库根目录 `E:\Company\demo\projects\project29` 打开 PowerShell：

```powershell
# 1) 生成确定性素材与 4 份清单（写入 demo/）
python demo/make_demo.py

# 2) 正常场景：3 段，gap_ms=80，其中第 2 段请求 [800,1200) 触发越界截取警告
python podcast_concat.py --manifest demo/manifest.json --output-dir demo/out --gap-ms 80
# Processed media/seg01_intro.wav: ok
# Processed media/seg02_host.wav: warning
# Processed media/seg03_guest.wav: ok
# 退出码 0；产物 demo/out/output.wav + demo/out/report.json

# 3) 坏素材场景：缺文件 + 采样率不符，后续片段照常处理
python podcast_concat.py --manifest demo/manifest_with_errors.json --output-dir demo/out_errors

# 4) 全部采样率不符：仍生成 0 帧合法 WAV
python podcast_concat.py --manifest demo/manifest_all_mismatch.json --output-dir demo/out_mismatch

# 5) 全静音输入：保持全 0 输出并给出静音警告
python podcast_concat.py --manifest demo/manifest_silent.json --output-dir demo/out_silent
```

查看报告（PowerShell）：

```powershell
Get-Content demo/out/report.json
```

或任意 JSON 工具解析，例如：

```powershell
python -c "import json;r=json.load(open('demo/out/report.json'));print(r['total_duration_ms'],r['counts'])"
```

## 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--manifest` | 必填 | 清单 JSON 路径 |
| `--output-dir` | `.` | 输出目录，不存在时自动创建；固定写入 `output.wav` 与 `report.json` |
| `--target-peak-db` | `-1` | 归一化目标峰值（dBFS） |
| `--gap-ms` | 清单的 `gap_ms`（缺省 0） | 覆盖清单中的段间静音时长 |

## 清单格式

```json
{
  "target_sample_rate": 44100,
  "gap_ms": 80,
  "segments": [
    {"file": "media/seg01_intro.wav", "start_ms": 0, "end_ms": 1000},
    {"file": "media/seg02_host.wav", "start_ms": 800, "end_ms": 1200}
  ]
}
```

- `start_ms`、`end_ms` 必须为非负整数且 `start_ms < end_ms`，否则整份清单判为无效。
- `file` 为相对路径时按**清单所在目录**解析。
- 清单无效时：打印错误到 stderr、退出码 2、不创建输出目录也不写 `output.wav`。

## 规则摘要

- 片段文件必须存在、单声道、16 位 PCM、采样率等于 `target_sample_rate`，否则该片段 `status="error"` 并写 `error` 原因；其音频被跳过，后续片段继续拼接。
- `[start_ms, end_ms)` 超出文件末尾 → 截取到 EOF，`status="warning"`，保留已截取音频。`start_ms` 已越过 EOF → 警告且该段贡献 0 帧。
- 仅 `ok`/`warning` 片段参与拼接，相邻有效片段之间插入 `gap_ms` 的 0 值样本。
- 峰值归一化对全部样本施加同一增益，使最大绝对样本值约为 `32767 * 10^(peak_db/20)`（默认 -1 dBFS 时为 29204，误差 ≤1）。
- 拼接结果全为 0 样本时不做增益，保持全静音，并在报告 `warnings` 中说明。
- 控制台按清单顺序逐段打印 `Processed <file>: <status>`（状态为 `ok`/`warning`/`error`）。

## 报告字段

顶层：`output_path`、`report_path`、`target_sample_rate`、`gap_ms`、`target_peak_db`、`total_duration_ms`、`total_frames`、`applied_gain`、`peak_amplitude`、`silent`、`counts`、`warnings`。

`segments[]` 每项：`index`、`file`、`resolved_path`、`start_ms`、`end_ms`、`status`（`ok`/`warning`/`error`）、`error`（无错为 `null`）、`warning`、`duration_ms`。

## 运行测试

```powershell
python -m unittest discover -s tests -v
```

测试共 18 个用例，覆盖需求 1–14，全部通过（本机 Python 3.10.20 / Windows 11）。

仓库中的 `demo/media/`、`demo/manifest*.json` 与 `demo/out*/` 都是 `demo/make_demo.py` 的生成产物，已写入 `.gitignore`，可随时删除并重新生成。

## 实际验证记录

| 场景 | 命令 | 结果 |
| --- | --- | --- |
| 3 段有效、含越界截取 | `demo/manifest.json` | 段数 3；第 2 段 200 ms 且 warning；总时长 2360 ms（1000+200+1000+2×80 gap）|
| 缺文件与采样率不符 | `demo/manifest_with_errors.json` | 2 ok / 2 error，错误原因非空；总时长 2080 ms = 1000+1000+80 gap，坏片段未污染输出 |
| 全部 48 kHz 素材对 44.1 kHz | `demo/manifest_all_mismatch.json` | 总时长 0；output.wav 可被 wave 读取（44100 Hz、1 声道、16 位、0 帧），逐条 error |
| 全静音 | `demo/manifest_silent.json` | 44100 帧全 0，`silent=true`，报告含静音警告 |
| 无效清单 | 手工写入 start_ms≥end_ms | stderr 报 `segments[0]` 错误，退出码 2，`demo/out_bad` 未创建 |
| 归一化 | 默认 `--target-peak-db -1` | 输出峰值 29204，与 32767×10^(-1/20)≈29204.6 相差 <1 |
| 目录自动创建 | `--output-dir` 指向不存在目录 | 目录被创建，内含 output.wav / report.json |
| `--gap-ms` 生效 | 3 段 + gap 0 对比 gap 100 | 总时长 300 → 500 ms，恰好多出 2 个间隔 |
