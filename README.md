# 播客 WAV 裁剪拼接工具（project55）

从多个 PCM WAV 源文件按 JSON 配置裁剪片段并按顺序拼接为单个节目 WAV，
输出参数（采样率 / 声道数 / 采样宽度）以**首个可用片段**为准；对参数不一致、
源文件缺失 / 损坏、区间越界等情况分别处理，并生成结构化 JSON 报告。

仅依赖 **Python 3.10+ 标准库**（`wave` / `json` / `tempfile` / `argparse`），
无第三方依赖，不使用 Docker。

## 目录结构

```
project55/
├── podcast_editor/
│   ├── __init__.py
│   └── wav_editor.py        # 主工具（配置校验 + 裁剪拼接 + 报告 + CLI）
├── scripts/
│   ├── make_samples.py      # 生成确定性 PCM WAV 测试样本
│   └── verify.py            # 端到端验收脚本（覆盖 8 条验收标准，23 项断言）
└── demo/                    # 业务演示（运行脚本后生成，已 gitignore）
```

## 一、本地启动方式

```powershell
# 1) 生成确定性样本（默认 samples/，演示用 demo/samples/）
python scripts/make_samples.py --out demo/samples

# 2) 准备配置 demo/config/program.json（格式见下）

# 3) 执行裁剪拼接
python -m podcast_editor.wav_editor `
  --config demo/config/program.json `
  --output demo/output/program.wav `
  --report demo/output/report.json
```

配置中的相对路径相对于**配置文件所在目录**解析；也支持绝对路径。
配置文件兼容带 / 不带 BOM 的 UTF-8。

### 配置格式

```json
{
  "clips": [
    {"source": "../samples/a.wav", "start": 0.0, "end": 2.0},
    {"source": "../samples/b.wav", "start": 2.0, "end": 9.0}
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `clips` | 必填，非空片段数组；缺失或为空直接报错、退出码 2 |
| `source` | 源 WAV 路径，必填非空字符串 |
| `start` | 起始秒，数字，≥ 0 |
| `end` | 结束秒，数字，必须 > `start` |

## 二、处理规则与退出码

| 情况 | 处理 | 报告分类 |
| --- | --- | --- |
| 区间完全在源文件内 | 正常裁剪拼接 | `complete_segments` |
| `end` 超过源文件时长 | 截取到源文件末尾 | `partial_segments`（给出原因） |
| 源文件缺失 | 跳过该片段 | `failed_segments` |
| 源文件损坏 / 非 PCM WAV / 不支持的压缩格式 | 跳过该片段 | `failed_segments` |
| 采样率 / 声道 / 采样宽度与首个可用片段不一致 | 跳过该片段 | `failed_segments` |
| `start` 已超出源文件时长（无可用音频） | 跳过该片段 | `failed_segments` |

退出码：

- `0`：至少一个片段成功（全部完整，或部分失败但有可用输出）
- `2`：配置本身非法（JSON 语法错、缺字段、片段列表为空、时间非法等），
  错误信息输出到 stderr
- `3`：全部片段失败，**不生成输出 WAV**，报告状态为 `failed`

## 三、报告格式（JSON）

```json
{
  "status": "partial",
  "output_wav": "demo/output/program.wav",
  "output_params": {"channels": 1, "sample_rate": 8000, "sample_width_bytes": 2},
  "total_output_frames": 32000,
  "total_output_duration_sec": 4.0,
  "counts": {"complete": 1, "partial": 1, "failed": 2, "total": 4},
  "complete_segments": [ {"index": 0, "reason": "裁剪成功，输出 2.000 秒（16000 帧）", ...} ],
  "partial_segments":  [ {"index": 2, "reason": "结束秒 9.000 超过源文件时长 4.000 秒，已截取到源文件末尾", ...} ],
  "failed_segments":   [ {"index": 1, "reason": "音频参数与首个可用片段不一致: ..."},
                         {"index": 3, "reason": "源文件缺失: ..."} ]
}
```

- 三个列表均按**配置顺序**（`index` 升序）排列；
- 每个条目都含片段 `index` 和**非空** `reason` 字符串；
- `status`：`success`（无失败）/ `partial`（有失败但产出了 WAV）/ `failed`（全部失败）。

## 四、业务操作样例（混合场景）

`demo/config/program.json`（4 个片段，覆盖完整 / 参数不一致 / 越界 / 缺失）：

```json
{
  "clips": [
    {"source": "../samples/a.wav",    "start": 0.0, "end": 2.0},
    {"source": "../samples/diff.wav", "start": 0.0, "end": 1.0},
    {"source": "../samples/b.wav",    "start": 2.0, "end": 9.0},
    {"source": "../samples/ghost.wav","start": 0.0, "end": 1.0}
  ]
}
```

实际运行结果：

- 片段 0：完整，2.0 s；
- 片段 1：`diff.wav` 为 16000 Hz 立体声，与首个片段（8000 Hz 单声道）不一致 → 失败跳过；
- 片段 2：`b.wav` 仅 4.0 s，结束秒 9.0 越界 → 部分完成，截取 2.0 s 到源末尾；
- 片段 3：文件不存在 → 失败跳过；
- 输出 `program.wav`：1 声道 / 8000 Hz / 16bit，共 4.0 s（32000 帧），退出码 0。

独立用标准库验证文件头：

```powershell
python -I -c "import wave; w=wave.open(r'demo/output/program.wav','rb'); print(w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getnframes()); w.close()"
# 1 8000 2 32000
```

全部失败场景（退出码 3、无 WAV）：

```powershell
python -m podcast_editor.wav_editor --config <全损坏配置> --output out/x.wav --report out/r.json
# 控制台：处理状态: failed / 全部片段失败，未生成输出 WAV
```

## 五、验收与实际验证结果

```powershell
python scripts/verify.py
```

该脚本自动生成样本并以真实子进程调用 CLI，覆盖任务全部 8 条验收标准，
共 **23 项断言，本地实际运行 23/23 全部通过**：

| # | 验收标准 | 验证方式 | 结果 |
| --- | --- | --- | --- |
| 1 | 总时长 = 各片段有效时长之和（含 PCM 字节级一致性比对） | t1_normal | PASS |
| 2 | 参数不一致片段跳过，输出参数同首个可用片段 | t2_param_mismatch | PASS |
| 3 | 源文件缺失跳过且其余片段正常拼接 | t3_missing | PASS |
| 4 | 结束秒越界截取到源末尾并标记 partial | t4_overlong | PASS |
| 5 | 报告三列表按配置顺序、条目含索引与非空原因 | t5_report + 全报告复核 | PASS |
| 6 | 全部失败：无 WAV、状态 failed、退出码非零（实测 3） | t6_all_failed | PASS |
| 7 | 空片段列表 / 缺字段 / 缺 clips：stderr 错误提示、退出码非零（实测 2） | t7_* | PASS |
| 8 | 标准 `wave` 可读且文件头与首个成功片段一致 | t8_header | PASS |

## 设计说明

- **帧对齐**：秒数按 `round(秒 × 采样率)` 换算为整数帧，所有时长均以帧为准。
- **原子输出**：先写同目录临时 WAV，处理完成后 `os.replace` 替换；全部失败时
  清理临时文件，目标路径不会留下半成品。
- **分块读写**：每块最多 1,048,576 帧，避免大文件一次性载入内存。
- **格式范围**：仅支持未压缩 PCM WAV（标准库 `wave` 可解析范围），压缩格式
  与损坏文件统一归类为“格式不支持或已损坏”的片段失败，不影响其他片段。
