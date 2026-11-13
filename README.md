# 播客后期剪辑工具（podcast_editor）

从单声道 16 位 PCM WAV 母带中按 JSON 清单毫秒级截取片段，统一调整到目标峰值
电平，按清单顺序合并为一个节目文件，并生成成功 / 跳过 / 静音分类的 JSON 报告。

- 仅使用 Python 3.10+ 标准库（`wave`、`array`、`json`、`argparse` 等），无第三方依赖
- 全部测试素材由脚本确定性生成，无需外部素材，不访问网络
- 不使用容器环境

## 目录文件

| 文件 | 作用 |
|---|---|
| `podcast_editor.py` | 主程序：校验输入、截取、峰值调整、合并、写报告 |
| `generate_fixtures.py` | 确定性生成测试 WAV（8k/16k/48k、立体声、8-bit、坏头、截断）与 JSON 清单 |
| `verify_acceptance.py` | 端到端验收脚本，覆盖任务书全部 9 条验收点（33 项断言） |

## 快速开始（可复现）

```powershell
# 1. 生成确定性素材（fixtures/ 目录，重复运行字节一致）
python -I generate_fixtures.py

# 2. 运行完整验收（33 项检查，全部 PASS 即通过）
python -I verify_acceptance.py

# 3. 业务演示：16 kHz 母带 + 混合清单，目标峰值 -6 dBFS
python -I podcast_editor.py fixtures\master_16000.wav fixtures\manifest_mixed.json demo_out -p -6
```

> `-I` 以隔离模式运行 Python，不加载当前目录下可能被植入的同名模块（如伪造的
> `json.py`），符合不可信素材目录的安全要求。

## 命令行

```
python podcast_editor.py <input_wav> <manifest.json> <output_dir>
                         [-p TARGET_PEAK_DBFS] [-m MERGED_NAME] [-r REPORT_PATH]
```

| 参数 | 说明 |
|---|---|
| `input_wav` | 单声道 16 位 PCM WAV，采样率须在 8000–48000 Hz |
| `manifest` | JSON 数组，每条含整型 `start_ms`、`end_ms`，可选 `output_name` |
| `output_dir` | 片段、合并文件与报告输出目录，不存在时自动创建（含多级嵌套） |
| `-p/--target-peak` | 目标峰值 dBFS，范围 -30 至 0，默认 -1（对应幅值 29204） |
| `-m/--merged-name` | 合并文件名，默认 `merged.wav`，位于输出目录内 |
| `-r/--report-path` | 报告路径，默认 `<output_dir>/report.json`；可指向输出目录之外 |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功（含存在跳过/静音条目的情况） |
| 2 | 命令行参数非法（峰值越界、非数值、缺参数），不产生任何输出 |
| 3 | 输入 WAV 被拒绝（头损坏、立体声、非 16-bit、采样率越界、数据截断），不产生任何输出 |
| 4 | 清单文件缺失或不是合法 JSON / 顶层结构非法 |
| 5 | 片段写入中途失败；已完成片段保留，外部报告标记 `manual_intervention_required` |
| 6 | 报告本身写入失败 |

## 清单格式

```json
[
  {"start_ms": 0, "end_ms": 500, "output_name": "intro.wav"},
  {"start_ms": 3200, "end_ms": 3600}
]
```

- 截取区间为半开区间 `[start_ms, end_ms)`；毫秒按四舍五入换算采样点
- `output_name` 缺省时自动命名 `clip_000.wav`、`clip_001.wav`……；路径分隔符会被净化
- 缺字段、字段非整型（含布尔值）、负起点、`end_ms <= start_ms`：该条跳过并在报告写明原因，其余条目继续
- `end_ms` 越过音频末尾：截到末尾并产生一条 warning；`start_ms` 不早于总时长：跳过并记录
- 全零片段：不做增益，原样输出，进入报告 `silence` 区且**不参与合并**

## 峰值调整规则

目标幅值 `A = round(32767 × 10^(dBFS/20))`，片段整体乘以 `A / 原峰值`（可衰减也可
提升），逐样本四舍五入并钳位到 [-32768, 32767]。默认 -1 dBFS → 29204。全零片段
峰值为 0，判定静音，跳过调整。

## 报告结构（report.json）

- `success`：按清单出现顺序排列，含 `output_name`、`duration_ms`、调整前后峰值、`overwritten`
- `skipped`：含 `entry_index`、非空 `reason`、原始条目快照
- `silence`：静音条目单独标记，附不参与合并的说明
- `warnings`：越界截断、覆盖文件、非法 output_name 等
- `merged_file` / `merged_duration_ms`：合并结果（无成功非静音条目时为 `null` / 0）
- 写入失败时：`manual_intervention_required: true` + `failure` 原因，且不生成合并文件

片段采用“同目录临时文件 + `os.replace` 原子替换”写入，写入中途失败不会留下半个
WAV，也不会破坏已存在的同名文件。

## 验收点与实测结果（2026-11-14，Python 3.10.20 / Windows 11）

`python -I verify_acceptance.py` 实测 **33/33 PASS**：

| 任务要求 | 验收场景 | 实测 |
|---|---|---|
| 1. 输入校验 | 8000/16000/48000 Hz 可读且采样率保持；坏 RIFF 头、立体声、8-bit、截断数据均退出码 3 且输出目录为空 | PASS |
| 2. 清单容错 | 混合清单 11 条：4 成功、1 静音、6 跳过；跳过原因均非空（缺字段/类型错/负时间/零长度） | PASS |
| 3. 区间处理 | `[3000,5000)` 越界截到 4000 ms（实测 1000 ms）；起点 9000 ms 超总时长被跳过并记录 | PASS |
| 4. 峰值调整 | -1/-3/-6 dBFS 实测偏差 0.0 dB；边界 -30 dBFS 实测 -30.002 dB、0 dBFS 实测 0.000 dB；全零片段原样输出并标记静音 | PASS |
| 5. 合并 | 合并时长 2400 ms = 500+500+1000+400 成功非静音之和；静音 500 ms 不参与；采样率 16000 Hz 与母带一致 | PASS |
| 6. JSON 报告 | 成功顺序与清单一致；跳过原因非空；静音在独立 `silence` 区 | PASS |
| 7. 目录与覆盖 | 多级不存在目录自动创建；预置同名片段被原子覆盖，报告含 `overwritten` 与覆盖 warning | PASS |
| 8. CLI 校验 | `-p -3` 峰值达标；`-p 5`、`-p abc`、`-p -30.5` 均退出码 2 且无输出目录/文件 | PASS |
| 9. 写入失败 | 只读目标触发 `WinError 5`，退出码 5；已完成 `clip_a.wav` 可正常打开；输出目录外报告列出成功条目并标记人工介入 | PASS |

## 典型业务操作样例

```powershell
# 默认 -1 dBFS
python -I podcast_editor.py fixtures\master_16000.wav fixtures\manifest_mixed.json run1

# 指定 -3 dBFS、自定义合并文件名与外部报告路径
python -I podcast_editor.py fixtures\master_16000.wav fixtures\manifest_one.json run2 `
  -p -3 -m episode_01.wav -r reports\run2_report.json

# 坏文件应被整体拒绝（输出目录不会被创建）
python -I podcast_editor.py fixtures\bad_header.wav fixtures\manifest_one.json run3
# ERROR: input WAV rejected: cannot read WAV header/frames: file does not start with RIFF id
```

## 确定性素材说明

`master_16000.wav`（4 秒，16 kHz，单声道 16-bit）内容固定：

| 区间 | 内容 | 峰值幅值 |
|---|---|---|
| 0.0–1.0 s | 440 Hz 正弦 | 20000 |
| 1.0–2.0 s | 440 Hz 正弦 | 8000 |
| 2.0–3.0 s | 全零静音 | 0 |
| 3.0–4.0 s | 440 Hz 正弦 | 30000 |

另生成 8 kHz/48 kHz 同结构母带、立体声、8-bit、坏头与截断 WAV，以及混合清单、
单片段清单、只读失败清单和坏 JSON 清单，供验收脚本使用。
