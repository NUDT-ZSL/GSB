# 过场动画帧序列导出包（frame_export）

把本地动画描述 JSON + 帧图目录转换成**可离线打开的导出包**，供导入引擎前核对每帧画面、时长与透明通道。

包内产物：

| 文件 | 内容 |
|------|------|
| `frames/*.png` | 按描述顺序合成后的 RGBA 帧，尺寸恒等于全局画幅 |
| `report.json` | 逐帧核对报告（序号、源文件、停留、尺寸处理、通道状态、结论） |
| `preview.html` | 逐帧浏览页面，内嵌时间线，断网可用，无外部依赖 |
| `.frame_export_manifest.json` | 记录本次写出/可清理的文件，重复运行不误删用户文件 |

---

## 1. 环境与安装

- Python 3.10+（本机实测 3.10.20）
- Pillow（本机实测 12.3.0）
- Windows / macOS / Linux 均可；**不使用容器**

```powershell
python -m pip install --upgrade Pillow
```

无需其他第三方依赖。所有路径可用相对或绝对路径。

## 2. 快速开始

```powershell
# 1) 生成确定性样例（JSON + 帧图，全部由代码生成，无外部附件）
python tools/make_sample.py

# 2) 导出“全部可用”样本
python -m frame_export samples/clean_storyboard/description.json exports/clean

# 3) 导出“含故障”样本（缺失文件 / 损坏 PNG / 负时长 / 重复与缺失序号）
python -m frame_export samples/storyboard_issues/description.json exports/issues

# 4) 打开预览
start exports/clean/preview.html
```

也可以直接使用双击友好的包装脚本：

```powershell
.\run_sample.ps1            # 生成样例 + 导出三个样本包
```

### 命令行

```
python -m frame_export <描述.json> <输出目录>
    [--resampling nearest|bilinear|bicubic|lanczos]   # 默认 lanczos
    [--timestamp]                                     # 报告写入生成时间（默认不写，保证可复现）
    [--force]                                         # 输出目录非本工具生成且非空时允许写入
```

退出码：

| 退出码 | 含义 |
|--------|------|
| `0` | 全部完整（complete） |
| `2` | 部分未完成（partial）——有帧失败或序号缺失/重复 |
| `3` | 无法导出（unexportable）——描述不可读，或没有任何帧成功 |
| `1` | 用法错误（参数缺失、输出目录被拒绝） |

## 3. 描述 JSON 格式

```json
{
  "canvas": { "width": 320, "height": 180 },
  "background": "#101820",
  "fps": 24,
  "frame_count": 3,
  "frames": [
    { "index": 0, "file": "frames/shot_k.png",    "duration_ms": 83 },
    { "index": 1, "file": "frames/b_reader.png",  "duration_ms": 125 },
    { "index": 2, "file": "frames/zeta_09.png",   "duration_ms": 42 }
  ]
}
```

- **顺序**：导出顺序严格按 `frames` 数组顺序，**与文件名、扩展名、字典序无关**。
- `canvas`：全局画幅，整数宽高，必须 > 0。兼容 `size` / `[宽,高]` / `"320x180"` / 顶层 `width`+`height`。
- `background`：`#RGB`、`#RRGGBB`、`#RRGGBBAA`、`[r,g,b]`、`[r,g,b,a]` 或常用颜色名；缺省为 `#000000` 并写入警告。
- `fps`：必须为大于 0 的有限数值；为 0、负数、非数值或缺失都会得到可读失败说明。
- `frame_count`：可选声明值；与实际条目数不一致会在报告与结论中体现。
- 帧文件引用：相对描述 JSON 所在目录的路径；绝对路径与 `..` 越界会被标记为无效帧而不读取。
- `duration_ms`：非负数值。负数、非数值、布尔值、缺失都会把**该帧**标为无效，其余帧继续导出。

## 4. 导出规则

| 情况 | 处理 |
|------|------|
| 源图 == 画幅 | 原样贴入（`as_is`） |
| 源图 < 画幅 | 居中放置，留白由**真实背景色像素**填充（`centered`） |
| 源图 > 画幅 | 等比缩小到画幅内（`downscaled`），不裁剪任何可见内容 |
| 无透明通道（RGB/L/CMYK…） | 转 RGBA，alpha 恒为 255，报告 `has_alpha_channel=false` 并注明 |
| 带 alpha 但全不透明 | 保留通道，报告注明“透明通道全部不透明” |
| EXIF Orientation | 按 EXIF 旋正后再合成，并在报告中记录 |
| 损坏/空/非图片 | 该帧 `corrupt`，记录原因，整批继续 |
| 文件不存在 | 该帧 `missing_file`，记录源引用，整批继续 |

合成使用 **RGBA paste**（替换像素而非 alpha 混合），因此源图的透明区域会露出配置的背景色，且不会残留画布旧像素。

## 5. 核对报告 `report.json`

顶层字段：

```
schema_version, generator{name,version,resampling}, source{description,frame_count_*},
canvas{width,height}, background{value,rgba}, fps, fps_raw,
verdict, verdict_label, verdict_reason,
summary{frames_total,frames_exported,frames_failed,frames_invalid,
        frames_missing_file,frames_corrupt,duration_ms_exported,
        duration_ms_all_entries,failed_indices},
sequence{order_source,missing_indices,duplicate_indices,out_of_order,declared_count_matches},
notes[], frames[]
```

逐帧字段（`frames[]`，顺序 = 描述顺序）：

```json
{
  "index": 1, "ordinal": 2, "source_file": "frames/b_reader.png",
  "duration_ms": 125, "duration_valid": true,
  "status": "ok",            // ok | invalid | missing_file | corrupt
  "usable": true, "error": null,
  "output": "frames/frame_0002_index_0001.png",
  "sha256": "…",
  "size_processing": {
    "source_size": [160, 90], "output_size": [320, 180],
    "placement": "centered", "placement_label": "居中留白", "scale": 1.0,
    "content_box": [80, 45, 240, 135],
    "padding": {"left": 80, "top": 45, "right": 80, "bottom": 45},
    "has_padding": true, "padding_sample_point": [0, 90],
    "padding_rgba": [16, 24, 32, 255], "padding_hex": "#101820FF"
  },
  "channel": {
    "source_mode": "RGBA", "source_format": "PNG",
    "has_alpha_channel": true, "alpha_range": [0, 255], "fully_opaque": false,
    "output_mode": "RGBA", "exif_orientation": null, "exif_applied": false
  },
  "notes": ["源图 160x90 小于画幅，居中留白 左80 上45 右80 下45 像素"]
}
```

**整批结论**（`verdict`，三类之一）：

| verdict | 条件 |
|---------|------|
| `complete` | 所有帧成功，且序号连续、无重复、声明帧数一致 |
| `partial` | 有帧成功也有帧失败，或序号存在缺失/重复/声明数不符 |
| `unexportable` | 描述不可读/非法，或没有任何帧成功 |

## 6. 预览页面 `preview.html`

- 按描述顺序排列全部帧卡片：合成图、源文件、停留毫秒、尺寸处理、通道状态、时间线起止。
- 顶部时间线按各帧 `duration_ms` 占比绘制，显示累计毫秒。
- 播放/暂停/回到第一帧，`←`/`→` 逐帧、空格播放；每帧按自身停留毫秒推进。
- 失败帧显示红框“未完成”与原因，**不会**伪造图片。
- 纯离线：无 `http(s)` 链接、无外链脚本、无网络字体。

## 7. 可重复性

- 连续运行两次，`report.json`、`preview.html` 与全部 `frames/*.png` **逐字节一致**（已实测，见 `VERIFICATION.md` 第 14 项）。
- 默认不写入生成时间；需要时间戳时加 `--timestamp`。
- 样本生成 `tools/make_sample.py` 亦为确定性：无随机数、无时间、无外部资源。

## 8. 测试与验收记录

```powershell
python -m unittest discover -s tests -v       # 56 项单元/验收测试
python tools/verify_requirements.py           # 14 条验收项端到端核对，生成 VERIFICATION.md
```

- `tests/`：按验收条目拆分的测试（顺序、留白、通道、缩放、时间线、无效时长/坏图、缺失/重复序号、报告结构、预览页、非法配置、可重复性、路径越界等）。
- `VERIFICATION.md`：实际运行结果与证据（14/14 通过）。
- `build/verification/`：核对脚本产出的三个导出包（可随时重建）。

## 9. 目录结构

```
frame_export/           # 实现
  spec.py               # 描述 JSON 解析与校验
  compose.py            # 解码 / 旋正 / 缩放 / 居中合成
  batch.py              # 顺序、序号分析、逐帧调度、结论
  report.py             # JSON 报告 + 离线预览页生成
  cli.py                # 命令行入口与包写出
tools/
  make_sample.py        # 确定性样本生成
  verify_requirements.py# 14 条验收端到端核对
tests/                  # unittest 验收测试
samples/                # 本地样例（代码生成）
exports/                # 示例导出包（clean / issues）
run_sample.ps1          # 一键生成 + 导出 + 打开预览
```

## 10. 已知边界与取舍

- 帧图引用只能指向描述 JSON 所在目录内部；这是防止导出包读取无关本地文件的安全约束。
- `--force` 仅在确认输出目录不是本工具产出的包时使用；工具只删除自身清单中记录的文件与 `frames/` 目录。
- 非法全局配置（fps/canvas/JSON 语法）不会留下空白结果：仍写出 `report.json`（`error` 字段 + `unexportable`）和解释性 `preview.html`，同时终端给出中文原因并以退出码 3 结束。
- 超大图受 Pillow 解压炸弹阈值保护（上限 2 亿像素），超限会作为单帧 `corrupt` 记录而不影响整批。
