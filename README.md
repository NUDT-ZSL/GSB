# 技能音效循环段裁剪与核对工具

按音频组标注清单（JSON）以**帧精度**裁剪循环段、生成首尾接缝试听片段，
并输出逐条核对报告（`report.json`），供音频人员判断素材可用或返工。

## 环境

- Windows 10/11，Python 3.10+（仅用标准库，无需 numpy、无需网络）

## 快速开始（Windows 命令行）

```bat
cd /d E:\Company\demo\projects\project91
python make_samples.py          :: 生成确定性样例 WAV 与 manifest.json（仅首次）
python loop_cutter.py manifest.json
```

一条命令即可完成导出与报告；可选 `--out <目录>` 指定输出位置（默认 `out/`）。
重复运行会清空并重建输出目录，不产生残留文件。

## 清单格式（manifest.json）

```json
{
  "entries": [
    {
      "id": "skill_fire",
      "wav": "samples/sine_a.wav",
      "sample_rate": 44100,
      "channels": 1,
      "loop_start_frame": 10000,
      "loop_end_frame": 20000,
      "preview_ms": 100
    }
  ]
}
```

- 循环区间为 `[loop_start_frame, loop_end_frame)`，按帧裁剪，不做按秒近似换算；
  导出帧数 = `loop_end_frame - loop_start_frame`。
- `wav` 相对清单所在目录解析，也可写绝对路径。

## 输出（out/）

| 文件 | 说明 |
|---|---|
| `<id>_loop.wav` | 循环段，采样率/声道数/位深与源文件一致 |
| `<id>_preview.wav` | 接缝试听：循环尾 N 帧 + 头 N 帧拼接 |
| `report.json` | 逐条核对报告，按 id 升序 |

试听拼接长度 N = `preview_ms` 按采样率换算为帧，**不超过循环段长度一半**，
越界自动截取并写入该条 `notes`。

## 报告字段

每条含：`id`、`status`（`ok`/`invalid`/`error`/`duplicate`）、`jump`
（接缝跳变量：循环末帧与首帧相邻样本差绝对值的各声道峰值）、`loop_wav`、
`preview_wav`、`loop_frames`、`preview_frames`、`reason`、`notes`；
无对应值时为 `null`。

- `invalid`：结束帧 ≤ 起始帧、起始帧为负或结束帧超出实际帧数 —— 单独列出，无导出。
- `error`：源 WAV 损坏、声明采样率/声道数与实际不符、缺少必需字段
  （原因中含清单条目序号与字段名）—— 不影响其他条目导出。
- `duplicate`：同一 id 重复出现 —— 仅保留先出现的一条导出结果。

控制台另按跳变量从小到大（并列按 id 稳定排序）打印有效循环，便于优先试听
接缝最明显的素材。

## 验收

```bat
python verify_acceptance.py
```

对任务 13 条要求逐项本地验证，全部通过时退出码为 0。
