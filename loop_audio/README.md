# 循环音效接缝评估与导出工具

音频设计师交付循环音效 WAV 与候选接缝点清单（JSON），本工具逐条评估接缝连续性，
选出最佳接缝并导出可直接供引擎循环播放的 WAV，同时产出处理报告。

## 环境要求

- Python 3.10+（代码仅使用 3.10 兼容语法；本机实际在 Python 3.14 验证通过）
- 依赖：`numpy`、`soundfile`（libsndfile）

```powershell
python -m pip install numpy soundfile
```

不使用容器；所有测试对象均为本目录下由脚本确定性构造的本地样例，不访问网络。

## 目录结构

```
loop_audio/
├── seam_processor.py   # 主处理程序（读清单 -> 评分 -> 选接缝 -> 交叉淡化导出 -> 报告）
├── make_samples.py     # 确定性构造本地样例 WAV 与 manifest.json
├── lock_helper.py      # 跨平台独占锁辅助脚本（验证“暂时不可读/重读补齐”）
├── check_demo.py       # 端到端演示与 12 项要求核验，生成 CHECK_RESULTS.md
├── CHECK_RESULTS.md    # 最近一次核验的实际检查结果（自动生成）
└── samples/            # 样例清单、WAV、输出与报告（make_samples 生成）
    ├── manifest.json
    ├── out/            # 导出的循环 WAV（清单 output_dir 指定）
    └── report.json     # 处理报告
```

## 启动说明

### 一键演示（推荐，覆盖全部 12 项要求）

```powershell
python -I check_demo.py
```

脚本会干净重建样例、独占锁定一个文件模拟“暂时不可读”、运行两轮处理器
（锁定一轮 + 解锁补齐一轮），逐条打印 PASS/FAIL 并写入 `CHECK_RESULTS.md`。
`-I` 为隔离模式（忽略用户 site 包与当前目录注入）；若环境第三方库装在 user site
导致 `-I` 下不可见，脚本会自动回退到普通模式。

### 单独运行处理器

```powershell
# 1. 生成样例（已生成可跳过）
python -I make_samples.py

# 2. 处理清单
python -I seam_processor.py --manifest samples\manifest.json --report samples\report.json
```

参数：

- `--manifest`：清单 JSON 路径（必填）
- `--report`：报告 JSON 路径（可选，默认清单同目录 `report.json`）

## 清单与报告格式

清单（UTF-8 JSON）：

```json
{
  "output_dir": "out",
  "entries": [
    {
      "file": "sine_tone.wav",
      "expected_loop_length": 44000,
      "candidate_seams": [10000, 22050, 50000]
    }
  ]
}
```

- `output_dir`：导出目录，相对路径相对于清单所在目录；不存在时自动创建。
- `file`：WAV 文件名，相对路径相对于清单所在目录。
- `expected_loop_length`：预期循环长度（采样数）。
- `candidate_seams`：候选接缝采样位置（升序评分时以清单顺序为并列裁决依据）。

报告为 JSON 数组，顺序与清单完全一致，每条固定包含五个字段：

| 字段 | 说明 |
| --- | --- |
| `file` | 清单中的文件名 |
| `status` | `ok` 或 `skipped` |
| `seam_index` | 选中候选在 `candidate_seams` 中的索引；跳过为 `null` |
| `seam_score` | 选中接缝得分；静音条目固定 `1.0`；其他跳过为 `null` |
| `reason` | 跳过原因；成功为空字符串 |

## 处理规则与设计说明

1. **格式准入**：仅接受 44100 Hz、16 位 PCM（`PCM_16`）、单声道 WAV；
   立体声/采样率/位深不符的条目跳过并在 `reason` 注明具体不符项。
2. **接缝得分**：对候选接缝 `s`，取前 5 ms 窗口 `[s-220, s)` 与后 5 ms 窗口
   `[s, s+220)`，得分 = `|RMS(前) − RMS(后)| / RMS(整段)`，RMS 按 16 位满量程归一化。
   连续信号前后能量接近，得分低；接缝跨电平突变时得分高。
3. **接缝选择**：按得分升序取最低者；得分完全相同取清单中靠前的候选。
4. **导出**：以选中接缝为循环起点，截取预期长度的循环段；在循环首尾（播放回绕处）
   做长度 441 采样（10 ms）的线性交叉淡化，淡化后采样数不变，输出时长与预期长度
   偏差为 0（要求 < 1 ms）；导出仍为 44100 Hz/16 位/单声道。
5. **跳过类规则**：
   - 文件缺失、损坏或被独占锁定（暂时不可读）：跳过并继续，可再次运行补齐；
   - 预期循环长度大于实际采样数：跳过，原因含“长度不足”；
   - 全零音频：`seam_score` 记 `1.0`，原因含“静音”，不产出导出文件；
   - 重复文件名：仅处理首个条目，后续条目原因含“重复”。
6. **进度输出**：每个条目向控制台打印一行 `[OK] 文件名` 或
   `[SKIPPED] 文件名 -> 原因`，便于业务人员逐条核对。

## 最近一次核验结论

见 `CHECK_RESULTS.md`：28/28 项检查通过。关键实测：

- 连续正弦接缝得分 `0.000000`（要求 < 0.2）；
- 突变接缝选中索引 `1`、得分 `1.085474`（要求 > 0.8，得分可大于 1，为真实 RMS 比值）；
- 突变音频回绕边界跳变经 10 ms 交叉淡化由 `0.7277` 降至 `0.000000`（满量程比），无爆音；
- 锁定条目首轮 skipped（原因含“不可读”），解锁重跑后 ok，既有成功条目不受影响。
