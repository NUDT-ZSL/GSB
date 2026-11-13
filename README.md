# 播客单集主干录音批量粗剪工具

仅依赖 Python 3.10+ 标准库（`wave` / `array` / `json` / `argparse` 等），无需第三方库、无需网络、不使用容器。

## 目录结构

```
project48/
├── podcast_editor/
│   └── podcast_editor.py      # 主处理程序 + 命令行入口
├── scripts/
│   ├── generate_fixtures.py   # 确定性素材/清单生成器（公式波形，重复运行字节一致）
│   └── run_acceptance.py      # 12 条验收点的黑盒验收脚本（共 18 个检查项）
├── acceptance_work/           # 验收脚本工作目录（运行后生成，可随时删除）
└── demo_run/                  # 手工业务演示输出（运行后生成）
```

## 剪辑清单格式

```json
{
  "main_recording": "../audio/main.wav",
  "clips": [
    {"clip_id": "c01", "type": "delete",      "start_sec": 1.0, "end_sec": 2.0},
    {"clip_id": "c02", "type": "insert_tone", "at_sec": 3.0,    "tone": "../audio/tone_a.wav"}
  ]
}
```

- `main_recording`：**相对路径**，仅允许一个主干；数组/对象形式（多主干）整体拒绝。
- 提示音 `tone`：**仅允许相对路径**（相对清单文件所在目录解析）；绝对路径（含 Windows 盘符/UNC）整体拒绝。
- `delete` 必填 `clip_id/type/start_sec/end_sec`；`insert_tone` 必填 `clip_id/type/at_sec/tone`。
- 整体性错误（JSON 非法、缺字段、`clip_id` 重复、绝对路径、多主干、目标峰值非法、主干缺失/不可读、输出不可写/同名成品已存在）→ 非零退出（退出码 2），**不生成成品也不生成报告**。
- 片段级错误（删除起点不早于终点、删除/插入越界、提示音缺失等）→ 仅该片段在报告中标记 `unprocessed` 并给出原因，其余片段照常成功。

## 命令行用法

```
python -I podcast_editor/podcast_editor.py \
  --manifest <清单.json> \
  --main <主干.wav> \
  --output-dir <输出目录> \
  --target-peak <1..32767 的整数目标峰值>
```

- 缺少任一必需参数时 argparse 输出 usage 并非零退出。
- 输出固定文件名：`podcast_output.wav`（成品）、`processing_report.json`（报告）。
- 同名成品/报告已存在时拒绝处理、**绝不覆盖**；文件以排他创建（`'x'` / `O_EXCL`）打开，是预检之外的第二道保险。
- 成品统一为单声道 16-bit PCM，采样率与主干一致；提示音采样率不同时按线性插值确定性重采样，所有样本钳制在 `[-32768, 32767]` 内。
- 峰值归一化按 `目标峰值 / 当前峰值` 整段缩放；静音素材不缩放。

报告中的 `ordering` 字段明确说明：`clips` 已按清单原始顺序排列，按 `manifest_index`（清单 `clips` 下标，从 0 开始）升序即可还原原始先后次序。

## 本地启动与演示（可复现）

### 1. 生成确定性素材

```powershell
python -I scripts/generate_fixtures.py --out acceptance_work/fixtures
```

生成内容：

| 文件 | 说明 |
| --- | --- |
| `audio/main.wav` | 主干 44100Hz/16bit/单声道，12.000s，混合正弦波 |
| `audio/tone_a.wav` | 提示音 A，44100Hz，1.000s |
| `audio/tone_b_22050.wav` | 提示音 B，**22050Hz**，0.500s（用于验证重采样） |
| `manifests/manifest_valid.json` | 正常清单：2 删除 + 2 插入 |
| `manifests/manifest_bad_clip.json` | 含起止倒置、越界删除片段 |
| `manifests/manifest_dup_id.json` | 重复 clip_id |
| `manifests/manifest_missing_field.json` | 缺必需字段 |
| `manifests/manifest_bad_json.json` | 非法 JSON |
| `manifests/manifest_abs_tone.json` | 绝对路径提示音 |
| `manifests/manifest_multi_main.json` | 多个主干 |

### 2. 运行完整验收（覆盖全部 12 条验收点）

```powershell
python -I scripts/run_acceptance.py
```

最近一次实测结果：**18 项检查，17 PASS / 0 FAIL / 1 SKIP**（SKIP 项为 POSIX 只读目录语义用例，Windows 平台目录只读位不阻止内部创建文件；Windows 真实不可写场景已用 ACL Deny Write 手工验证通过，见下）。

### 3. 手工业务样例

正常清单（时长预期 = 12 − 2 个删除各 1s + 提示音 1.0s + 重采样提示音 0.5s = 11.5s）：

```powershell
python -I podcast_editor/podcast_editor.py `
  --manifest acceptance_work/fixtures/manifests/manifest_valid.json `
  --main acceptance_work/fixtures/audio/main.wav `
  --output-dir demo_run/valid `
  --target-peak 20000
```

混入坏片段（坏片段未处理、其余成功，成功片段信息保留在报告中）：

```powershell
python -I podcast_editor/podcast_editor.py `
  --manifest acceptance_work/fixtures/manifests/manifest_bad_clip.json `
  --main acceptance_work/fixtures/audio/main.wav `
  --output-dir demo_run/mixed `
  --target-peak 18000
```

整体拒绝类样例（均非零退出、无产物）：

```powershell
# 重复 clip_id
python -I podcast_editor/podcast_editor.py --manifest acceptance_work/fixtures/manifests/manifest_dup_id.json --main acceptance_work/fixtures/audio/main.wav --output-dir demo_run/dup --target-peak 20000
# 非法目标峰值
python -I podcast_editor/podcast_editor.py --manifest acceptance_work/fixtures/manifests/manifest_valid.json --main acceptance_work/fixtures/audio/main.wav --output-dir demo_run/badpeak --target-peak 0
# 主干不存在（错误信息含完整路径）
python -I podcast_editor/podcast_editor.py --manifest acceptance_work/fixtures/manifests/manifest_valid.json --main acceptance_work/fixtures/audio/missing.wav --output-dir demo_run/nomain --target-peak 20000
```

## 已实测的关键验证记录

- 正常清单成品：44100Hz / 16-bit / 单声道，时长 11.500000s 与公式预期逐样本相等，无越界样本。
- 22050Hz 提示音重采样后在报告中记录 `tone_native_rate=22050, resampled_to_rate=44100`，插入 0.500s。
- 目标峰值 20000 下成品实测峰值恰好 20000，`within_target=true`；峰值参数 0 与 99999 均被拒绝。
- 同目录二次运行：退出码 2 提示「同名成品已存在，拒绝覆盖」，原 WAV 哈希不变。
- Windows ACL 对输出目录设置 Deny Write 后：探针报 `Permission denied`、退出码 2、目录内零产物。
- 相同输入与参数两次运行：成品 WAV 的 SHA-256 完全一致；报告在归一化机器路径字段后内容完全一致。
- 确定性来源：波形由固定数学公式生成；重采样用纯浮点线性插值 + 固定舍入规则；报告以 `newline='\n'`、固定缩进写出，不含时间戳/随机数。
