# 本地化配音小样批量剪辑工具（dub_cutter）

仅依赖 **Python 3.10+ 标准库**（`wave` / `array` / `json` / `argparse` 等），
无需第三方包、无需网络、无需 Docker，Windows 终端直接运行。

## 目录文件

| 文件 | 作用 |
|---|---|
| `dub_cutter.py` | 主程序：读清单 → 两级校验 → 切片/归一化/限幅 → 写 WAV 与 report.json |
| `make_sample.py` | 确定性生成 `sample.wav`（44.1kHz/16bit/单声道，3 秒）与示例 `edit_list.json` |
| `verify_all.py` | 九条验收标准的自动化验证（临时目录隔离，真实子进程跑 CLI） |

## 一、本地启动方式（可复现）

```powershell
# 1. 确认 Python 版本（需 3.10+）
python --version

# 2. 生成确定性样本（幂等，可重复执行）
python make_sample.py
# -> sample.wav（132300 帧）+ edit_list.json（3 条示例）

# 3. 先 dry-run 只校验、不落盘
python dub_cutter.py --dry-run

# 4. 正式执行（默认读 sample.wav / edit_list.json，产物在 .\output）
python dub_cutter.py

# 5. 一键跑九条验收（全部通过时退出码 0）
python verify_all.py
```

也可显式指定路径：

```powershell
python dub_cutter.py --wav sample.wav --edit-list edit_list.json `
                     --output-dir output --report output\report.json
```

## 二、edit_list.json 格式

```json
[
  {"id": "line_01", "start_ms": 100, "end_ms": 900, "target_dbfs": -6.0}
]
```

- `id`：片段标识，同时决定输出文件名 `<id>.wav`，须为合法 Windows 文件名。
- `start_ms` / `end_ms`：非负整数毫秒，且 `start_ms < end_ms`。
- `target_dbfs`：目标峰值电平（dBFS，有限数值，0 即满幅）。

## 三、校验分层与退出码

| 层级 | 情形 | 行为 | 退出码 |
|---|---|---|---|
| 致命（清单级） | JSON 非法、字段缺失、起止非非负整数、起止倒置、终点超出录音总时长 | 整体拒绝，**不写任何 WAV，也不写 report.json**，终端打印中文原因 | `2` |
| 条目级 | id 重复、id 含 `<>:"/\|?*` 等非法字符/保留名 | 仅拒绝该条，其余正常产出，报告记录原因 | 有拒绝时 `1` |
| 运行级 | 单条写盘失败（权限/占用等） | 原子写保证不留半截文件，其余片段继续产出 | 有失败时 `1` |
| 全部成功 | — | — | `0` |

## 四、关键实现约定

1. **毫秒→帧**：`frame = ms * sample_rate // 1000`（整除），切片首帧即清单起点对应帧。
   例：1ms @44100 → 第 44 帧，输出首帧样本值与源第 44 帧完全一致。
2. **归一化**：以片段峰值为基准计算增益
   `gain = 32767 * 10^(target_dbfs/20) / 源峰值`，逐样本四舍五入后硬限幅到
   `[-32768, 32767]`，输出峰值绝不超过 16bit 满幅；全静音片段原样输出。
3. **输出格式**：恒为 16bit、单声道、采样率与源文件一致。
4. **原子写**：先写同目录 `*.tmp.wav` 再 `os.replace` 替换，失败即清理临时文件，
   不会污染已成功的片段数据。
5. **report.json**：`entries` 数组顺序与清单完全一致，每条含 `id`、`status`
   （`success` / `rejected` / `failed`）、`output_frames`、`reason` 等；
   顶层另有 `success_count` / `rejected_count` / `failed_count`
   与 `all_writes_succeeded`、`all_entries_exported` 汇总字段。
6. **dry-run**：不创建输出目录、不写音频与报告，仅按清单顺序打印将产出的
   `<id>.wav` 片段名（被拒条目同步打印原因）。

## 五、业务操作样例（已实际运行）

```powershell
# 场景 A：混合清单（1 合法 + 重复 id + 非法字符 id）
# 结果：仅 scene2_ok.wav 产出，重复/非法条目被拒，exit code = 1
python dub_cutter.py --wav demo_mixed\sample.wav `
                     --edit-list demo_mixed\edit_list.json `
                     --output-dir demo_mixed\output

# 场景 B：起止倒置 -> 整体拒绝，output 目录不存在，exit code = 2
python dub_cutter.py --wav demo_fatal\sample.wav `
                     --edit-list demo_fatal\edit_list.json `
                     --output-dir demo_fatal\output

# 场景 C：dry-run -> 目录无任何新增文件，终端按序列片段名
python dub_cutter.py --dry-run
```

## 六、实际验证结果（2026-11-14，Python 3.10.20 / Windows 11）

`python verify_all.py` 输出 **9/9 通过**：

| # | 验收点 | 实测结果 |
|---|---|---|
| 1 | 起止倒置整体拒绝 | exit=2，无任何片段 WAV / output 目录 |
| 2 | 终点越界整体拒绝 | exit=2，report.json 不存在，终端提示超出帧数 |
| 3 | 毫秒换帧首帧对齐 | 1ms 起点输出首帧=源第 44 帧=12253，第二帧=源第 45 帧 |
| 4 | 0dBFS 归一化限幅 | 源峰值 2000 → 输出峰值 32767，41876 个非零样本，无越界 |
| 5 | WAV 头格式 | channels=1, sampwidth=2, framerate=44100 |
| 6 | 重复/非法 id 条目级拒绝 | 仅合法 `ok_seg.wav` 存在 |
| 7 | 报告顺序与原因 | 条目顺序与清单一致，被拒条目 status=rejected 且 reason 非空 |
| 8 | 写盘失败隔离 | 其余两条 WAV 可读，失败条 status=failed，无 .tmp 残留，exit=1 |
| 9 | dry-run 零落盘 | 目录无变化，按序列出 dry_one.wav、dry_two.wav |
