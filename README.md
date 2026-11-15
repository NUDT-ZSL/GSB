# 动画帧条带合成与核对工具

按动画清单（JSON）将逐帧 PNG **按序号水平拼接**为保留透明通道的条带预览图（PNG），
并输出逐动画的核对结果（JSON），用于核对帧时长配平与透明区域。

- 纯 Python 标准库 + Pillow，**不使用容器**、无外部素材
- 样本 PNG 与清单由 `tools/generate_sample.py` **确定性生成**（固定种子，可随时重建）
- 约束：动画 ≤ 40 个、每动画 ≤ 60 帧、单帧 ≤ 512×512
- 测试仅针对本地临时目录中的生成样本

## 目录结构

```
project110/
├── animstrip/            # 核心包
│   ├── models.py         # 数据模型与路径解析（相对清单目录/绝对路径）
│   ├── parser.py         # 清单加载与结构校验（目标时长、重复序号等）
│   ├── builder.py        # 条带合成（RGBA，alpha_composite，保留原 alpha）
│   ├── pipeline.py       # 主流程 + result.json + 过期条带清理
│   ├── samplegen.py      # 确定性样本生成逻辑
│   └── cli.py            # 命令行入口
├── tools/generate_sample.py   # 样本生成脚本
├── tests/ + conftest.py       # 25 项 pytest 用例，逐条对应 8 条需求
├── requirements.txt
└── sample/                     # 生成产物：清单 + 逐帧 PNG（可重建）
```

## 一、本地启动步骤

要求 Python ≥ 3.10。Windows PowerShell：

```powershell
# 1. 创建并激活虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. 安装 Pillow 10.x
pip install -r requirements.txt

# 3. 确定性生成样本（固定种子，重复生成逐字节一致）
python tools/generate_sample.py sample

# 4. 合成条带并输出核对结果
python -m animstrip.cli sample/manifest.json -o strip_output
```

输出：

- `strip_output/<动画ID>.png`：RGBA 条带预览图（仅合成成功的动画）
- `strip_output/result.json`：逐动画核对结果，按动画 ID 排序

## 二、清单格式与业务样例

```json
{
  "animations": [
    {
      "animation_id": "anim_alpha",
      "target_duration_ms": 300,
      "frames": [
        {"index": 0, "image": "frames/anim_alpha/f0.png", "duration_ms": 100},
        {"index": 1, "image": "frames/anim_alpha/f1.png", "duration_ms": 100},
        {"index": 2, "image": "frames/anim_alpha/f2.png", "duration_ms": 100}
      ]
    }
  ]
}
```

字段说明：

- `animation_id`：动画 ID，同时作为条带文件名（`<id>.png`），不得含路径分隔符
- `target_duration_ms`：目标帧时长（毫秒），必须为正数，否则条目被拒、不进入合成
- `frames[].index`：帧序号，合成时按其升序排列；同动画内重复即拒绝
- `frames[].image`：图片路径，**相对路径按清单所在目录解析**（支持子目录），绝对路径原样定位
- `frames[].duration_ms`：该帧停留毫秒数（非负整数）；各帧之和与目标时长的差值写入核对结果

样本清单覆盖 9 个动画（正常与异常混跑）：

| 动画 ID | 场景 | 预期结果 |
|---|---|---|
| `anim_alpha` | 配平 + 全透明/半透明像素，子目录相对路径 | 通过，3 帧条带 |
| `anim_mixsize` | 帧高不一致、清单内乱序 | 通过，按序号排序、垂直居中 |
| `anim_loop` | 绝对路径写法 | 通过，3 帧条带 |
| `anim_drift` | 停留和 450ms vs 目标 400ms | 出条带但不通过，`diff_ms = 50` |
| `anim_missing` | 一帧文件缺失 | 不出条带，原因“帧图片缺失” |
| `anim_corrupt` | 文件无法解码 | 不出条带，原因“无法解码” |
| `anim_bad_dur` / `anim_bad_dur_neg` | 目标时长 0 / 负数 | 结构校验拒绝 |
| `anim_dup_index` | 帧序号重复 | 结构校验拒绝 |

## 三、核对结果 JSON（result.json）

逐动画字段齐全并按 `animation_id` 排序：

```json
{
  "animation_id": "anim_drift",
  "target_duration_ms": 400,
  "frame_count": 4,
  "actual_total_ms": 450,
  "diff_ms": 50,
  "passed": false,
  "strip": "anim_drift.png",
  "issues": ["时长配平偏差 50 毫秒（实际 450 / 目标 400）"]
}
```

被拒/失败条目 `strip` 为 `null`，`issues` 给出具体原因；条带字段与输出目录中的 PNG 一一对应。

## 四、验证记录

### 测试

```powershell
pip install pytest
python -m pytest tests/ -v
```

结果：**25 passed**（Python 3.10.20 + Pillow 10.4.0，Windows 11）。

### 8 条需求与用例对应关系

| # | 需求 | 对应用例 |
|---|---|---|
| 1 | 条带宽度=各帧宽度之和、帧序按序号 | `tests/test_strip_layout.py`（3 项，含乱序与混合高度） |
| 2 | 时长差值与手算一致、单位毫秒 | `tests/test_duration_check.py`（3 项，手算 450−400=50） |
| 3 | 半透明/全透明 alpha 原样保留 | `tests/test_alpha_preserved.py`（3 项，逐像素比对） |
| 4 | 缺帧/坏帧仅该动画缺席并说明原因 | `tests/test_missing_frames.py`（4 项，含动态删帧） |
| 5 | 相对（子目录）与绝对路径均可定位 | `tests/test_path_resolution.py`（2 项） |
| 6 | 目标时长非正/序号重复被拒且不合成 | `tests/test_manifest_validation.py`（3 项） |
| 7 | JSON 字段齐全、按 ID 排序、与条带对应 | `tests/test_report.py`（4 项） |
| 8 | 重复运行逐字节一致、不残留过期条带 | `tests/test_determinism.py`（3 项，sha256 比对） |

### 端到端实测摘录

- CLI 输出：`共 9 个动画，产出 4 张条带，通过 3 个`
- 像素核验：`anim_alpha.png` 宽度 144 = 48×3；模式 RGBA；条带 alpha 集合 `{0, 128, 255}`，与源帧逐像素一致
- 连跑两次：输出文件集合与 sha256 完全一致；预置的过期条带 `anim_ghost.png` 在下次运行时被清除

## 设计要点

- 条带以 `Image.new("RGBA", ..., (0,0,0,0))` 全透明画布 + `alpha_composite` 合成，
  全透明像素保持 alpha=0，绝不铺白/黑底色；不同帧高时垂直居中、透明补边
- 输出目录中的 `*.png` 完全由本次运行托管，运行结束即清除本次未产出的旧条带
- PNG 以无损默认参数保存，固定压缩级别保证重复运行逐字节一致
