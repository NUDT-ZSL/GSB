# podcutter — 播客裁剪工具

把一期单声道 16 位 PCM WAV 按 JSON 裁剪列表切成多个片段，输出 WAV 文件和一份 JSON 报告。仅使用 Python 3.10+ 标准库，无第三方依赖。

## 本地启动

```powershell
# 需要 Python 3.10+
python --version

# 跑完整测试（47 个用例，全部使用确定性生成的样本）
python -m unittest test_podcutter -v

# 跑端到端演示（自动生成样本并逐条校验 9 项需求，产物留在 demo_output/）
python demo.py
```

业务调用：

```powershell
python podcutter.py --wav episode.wav --cuts cuts.json --out clips --report report.json
```

| 参数 | 含义 |
| --- | --- |
| `--wav` | 输入 WAV，必须是单声道 16 位 PCM |
| `--cuts` | 裁剪列表 JSON |
| `--out` | 输出目录，不存在时自动创建 |
| `--report` | 报告路径，默认 `report.json` |

退出码：`0` 全部成功，`1` 部分片段失败/无效，`2` 整体错误（配置、文件、格式），`3` 报告本身写不出去。

## 输入样例

`cuts.json`：

```json
[
  {"name": "intro",   "start": 0.5, "end": 1.0},
  {"name": "loud",    "start": 1.5, "end": 2.0, "gain": 20},
  {"name": "bad_range", "start": 5, "end": 3},
  {"name": "outro",   "start": 3.0, "end": 4.0, "gain": -6}
]
```

字段约束：

- `name`：仅字母、数字、下划线，列表内唯一；输出文件即为 `<name>.wav`。
- `start` / `end`：有限数字（秒），要求 `0 <= start < end <= 文件时长`。
- `gain`：可选，数字（dB），缺省为 0。

## 行为说明

- **格式**：声道数 ≠ 1、位深 ≠ 16 或非 PCM，整体终止，报告 `UNSUPPORTED_WAV_FORMAT`，不创建输出目录。
- **配置错误**：非法/重复名称、缺字段、类型不符、JSON 语法错误，整体终止，报告 `CONFIG_ERROR` / `INVALID_JSON`，不产生任何片段。
- **文件错误**：输入不存在、不是普通文件或不可读，整体终止，报告 `FILE_NOT_FOUND` / `FILE_NOT_READABLE`。
- **单片段范围无效**：该片段标记 `invalid` 并继续处理其余片段，不影响其他输出。
- **采样边界**：`start_sample = round(start × 采样率)`，`end_sample = round(end × 采样率)`，取半开区间 `[start_sample, end_sample)`；0 dB 时输出与原始采样逐点一致。
- **增益**：`round(sample × 10^(dB/20))`，按 16 位范围 `[-32768, 32767]` 饱和裁剪。
- **覆盖**：同名旧文件会被替换；先写临时文件再 `os.replace`，失败不会留下半成品或临时文件。
- **片段隔离**：单个片段写出失败时标记 `failed` 并继续，报告中给出片段名和可读原因。
- **报告**：`segments` 严格按输入顺序排列；成功项含 `output_file` 与 `duration_seconds`，失败/无效项这两个字段为 `null` 且 `error` 为说明文本。

报告顶层结构：

```json
{
  "status": "success | partial | error",
  "error": null,
  "inputs": {"wav": "...", "cuts": "...", "output_dir": "..."},
  "audio": {"sample_rate": 8000, "channels": 1, "bits_per_sample": 16,
            "total_frames": 32000, "duration_seconds": 4.0},
  "segments": [
    {"name": "intro", "status": "success", "output_file": "intro.wav",
     "duration_seconds": 0.5, "error": null, "start": 0.5, "end": 1.0,
     "gain_db": 0.0, "start_sample": 4000, "end_sample": 8000, "frames": 4000}
  ],
  "summary": {"total": 5, "success": 3, "failed": 0, "invalid": 2, "skipped": 0}
}
```

`status` 为 `error` 时，`error` 是 `{"code": "...", "message": "..."}`；格式不受支持时仍会按输入顺序列出全部片段并标记 `skipped`，便于确认没有任何输出。

## 实际验证结果

`python demo.py` 的五个场景全部通过（脚本会逐项打印 PASS/FAIL 并以退出码汇报）：

| 场景 | 覆盖需求 | 实际结果 |
| --- | --- | --- |
| A 正常裁剪 + 增益 + 越界 | 2/3/5/8 | 退出码 1；`intro` 覆盖了预置旧文件，采样逐点等于原始 `[4000:8000]`；`loud` +20 dB 等于原始 ×10；`outro` -6 dB 与手算一致；两个越界片段 `invalid` 且无文件 |
| B 配置错误 | 4/6 | 重复名称、非法字符、缺 `end`、`start` 为字符串均退出码 2，错误信息可读，输出目录为空 |
| C 输入缺失 | 9 | `FILE_NOT_FOUND`，明确点名缺失路径，无输出 |
| D 非单声道 16 位 PCM | 1 | 双声道与 8 位输入均 `UNSUPPORTED_WAV_FORMAT`，片段标记 `skipped`，无输出 |
| E 单片段失败隔离 | 7 | 用同名目录制造写失败：`boom` 标记 `failed` 并带原因，`first` / `last` 内容与手算一致，无临时文件残留 |

测试套件 `python -m unittest test_podcutter -v`：47 个用例全部通过，覆盖 9 项需求及其边界（负起点、零长度、亚采样间隔、`end` 恰好等于文件时长、NaN 增益、大小写输出冲突、原子覆盖等）。

## 文件

- `podcutter.py` — 实现与 CLI
- `test_podcutter.py` — 47 个确定性单元测试
- `demo.py` — 一键生成样本并校验全部验收场景（产物在 `demo_output/`，已被忽略）
