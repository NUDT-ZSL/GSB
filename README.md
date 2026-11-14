# 二维纸片模型离线遮挡预览（project68）

在美术交付前，用本地场景 JSON + PNG 贴图离线合成二维纸片模型的遮挡暗部预览。
纯 Python + Pillow，不依赖实时渲染器、GPU、网络或容器；同一输入重复运行输出逐像素一致。

## 环境

- Python 3.10+
- Pillow（已在 Python 3.14.7 / Pillow 12.3.0 上验证）

```powershell
pip install Pillow
```

## 快速开始

```powershell
# 1) 生成确定性的样例贴图与场景（可重复执行，输出字节一致）
python tools/make_samples.py

# 2) 合成基础场景
python paper_preview.py --scene samples/cases/base.json --out out/base

# 3) 逐条验收（会调用上面的脚本并逐像素核对，打印 PASS/FAIL 与实测值）
python tools/verify_acceptance.py
```

合成产物：

- `out/<name>/preview.png`：画布尺寸的合成预览（RGB）
- `out/<name>/report.json`：按层级顺序的处理报告
- 可选 `--mesh-previews DIR`：额外输出每个已合成网格的单独阴影图层，便于逐网格核对遮挡效果

退出码：`0` 完成（含部分完成），`2` 场景整体被拒绝，`1` 读写失败。

## 场景 JSON 格式

```json
{
  "canvas": { "width": 240, "height": 160, "background": "#202830" },
  "occlusion_strength": 0.65,
  "meshes": [
    { "name": "back",  "layer": 1, "x": 8,  "y": 48, "texture": "textures/back.png" },
    { "name": "front", "layer": 3, "x": 120, "y": 16, "texture": "textures/front.png" }
  ]
}
```

- `canvas.width` / `canvas.height` / `canvas.background`、`canvas`、`meshes` 为必需字段。
- 每个网格的 `x`、`y`、`layer`、`texture` 为必需字段；缺任一字段的网格不参与合成，并在报告中给出原因。
- `layer` 为整数，数字越大越靠前；相同 `layer` 的网格视为同一层级，彼此不产生遮挡。
- `x`/`y` 为像素坐标，允许小数（四舍五入）与负数；绘制区域超出画布时只保留交集。
- `texture` 为相对场景 JSON 所在目录的路径（也接受绝对路径）。
- `occlusion_strength` 可选，0–1，默认 0.65；100% 遮挡时像素亮度乘以 `1 - 0.65 = 0.35`。

## 处理规则

| 情形 | 行为 |
| --- | --- |
| 画布宽/高非数值、非整数、≤0，或背景色非法 | 拒绝整个场景：不写 `preview.png`，报告 `status: "rejected"` 并给出可读原因 |
| 场景/画布缺少必需字段、JSON 解析失败 | 同上，拒绝整个场景 |
| 网格缺少 `layer`/`x`/`y`/`texture` | 仅跳过该网格，其余照常合成 |
| 网格坐标非数值、NaN/Inf、布尔 | 仅跳过该网格 |
| 贴图文件不存在或损坏 | 仅跳过该网格，报告写入文件名与原因 |
| 网格完全在画布外 | 仅跳过该网格，报告说明无交集 |
| 网格部分越界 | 裁剪后正常参与合成与遮挡计算，报告 `partially_clipped: true` |

遮挡计算：对每个已合成网格，取所有更高层级网格 alpha 的逐像素合成结果作为遮挡度
（0–255），遮挡越强压暗越多，遮挡为 0 的像素严格保持原色；最前层自身遮挡度恒为 0。
报告为每个已合成网格给出 `opaque_pixels`、`occluded_pixels`、`occluded_pixel_ratio`、
`mean_occlusion` 供复核。

## 报告字段

```json
{
  "scene": "base.json",
  "status": "completed | partial | rejected",
  "canvas": { "width": 240, "height": 160, "background": "#202830" },
  "occlusion_strength": 0.65,
  "meshes": [
    {
      "order": 0, "index": 0, "name": "back", "layer": 1,
      "texture": "../textures/back.png",
      "status": "composited", "partially_clipped": false,
      "texture_size": [96, 72],
      "full_rect": [8, 48, 104, 120], "placed_rect": [8, 48, 104, 120],
      "source_rect": [0, 0, 96, 72],
      "opaque_pixels": 6912, "occluded_pixels": 1600,
      "occluded_pixel_ratio": 0.2315, "mean_occlusion": 0.2315
    },
    {
      "order": 3, "index": 3, "name": "no_layer", "layer": null,
      "texture": "../textures/back.png",
      "status": "skipped", "partially_clipped": false,
      "unprocessed_reason": "缺少必需字段: layer"
    }
  ],
  "summary": { "total": 4, "composited": 3, "skipped": 1 }
}
```

- `meshes` 严格按 `layer` 升序（后到前）排列；缺 `layer` 的条目排在末尾，保持原始顺序。
- 成功条目不含 `unprocessed_reason`；`status` 为 `composited` 或 `skipped`，未完成原因可读。
- 顶层 `status`：全部网格成功为 `completed`，有跳过项为 `partial`，场景整体被拒绝为 `rejected`。
- 字段顺序固定，JSON 以 UTF-8 + `\n` 写出，便于人工 diff。

## 本地验证结果

`python tools/verify_acceptance.py` 实测输出（24 项断言全部通过）：

| 要求 | 断言 | 实测 |
| --- | --- | --- |
| 1 缺层级字段 | 网格不出现在预览，报告列出原因 | `skipped`，`缺少必需字段: layer`；预览与基准图逐像素一致 |
| 2 尺寸与前后覆盖 | PNG 尺寸=画布；重叠像素=前层贴图颜色 | 240x160；`(179,74,74)` 与 front.png 同点一致 |
| 3 遮挡暗部 | 被遮挡区域明显更暗；前层自身不受影响 | 实心遮挡亮度 150.0 → 52.0；半透明遮罩下暗化比 0.9567 → 0.6753 → 0.4007 单调递增；front `occluded_pixels=0` |
| 4 贴图缺失 | 跳过该网格，其余正常；报告含文件名与原因 | `贴图文件不存在: ../textures/missing_middle.png`，其余像素正常 |
| 5 越界裁剪 | 只裁越界部分，报告标注部分裁剪 | `placed_rect=[220,16,240,112]`，`partially_clipped=true`，保留部分像素正常 |
| 6 画布非法 | 拒绝整个场景，不产出预览 | `status=rejected`，`画布宽度非法：必须大于 0（收到 0）`，无 preview.png |
| 6 网格坐标非法 | 仅跳过该网格 | `坐标 x 非法：必须为有限数值（收到 "abc"）`，其余 3 个网格成功 |
| 7 报告分类 | 按层级顺序标注三类状态 | layers `[1,2,3]`，状态依次 `composited` / `skipped` / `skipped`，成功条目无 `unprocessed_reason` |
| 8 可复现 | 两次运行 PNG 与报告一致 | PNG 字节一致（768 B），报告 2101 字符完全一致 |

验收产物位于 `out/acceptance/`；其中 `out/acceptance/base_meshes/`、`out/acceptance/partial_meshes/`
是逐网格阴影图层，可直接打开对比被遮挡前后的明暗。

## 目录

```
paper_preview.py            主程序
tools/make_samples.py       生成确定性贴图与验收场景
tools/verify_acceptance.py  逐条验收脚本（逐像素断言）
samples/textures/           生成的小尺寸贴图
samples/cases/              验收场景 JSON
out/                        运行产物（已忽略）
```
