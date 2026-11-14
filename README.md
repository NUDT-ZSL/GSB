# 二维图层合成预检工具（project94）

美术交付二维图层合成包前，用本工具把图层清单 JSON 与 PNG 目录按 `order` 叠放，
输出**合成预览图**、**逐层裁剪预览**与**核对报告 JSON**，供美术定位返工范围。

- 运行时只依赖 **Python 3.10+** 与 **Pillow 10.0+**（`pillow>=10.0`）。
- 全部处理在本机完成，不联网、不使用容器。
- 合成在**直通 alpha（straight alpha）**空间按 W3C compositing 公式计算，
  normal / multiply 结果与常见 DCC 软件一致。

## 1. 安装

```bash
python -m pip install "Pillow>=10.0"
python --version          # 需要 3.10 及以上
python -c "import PIL; print(PIL.__version__)"
```

可选：`python -m pip install pytest`（仅运行自动化测试时需要）。

## 2. 快速开始（本地可复现样例）

```bash
# 1) 用固定种子生成全部样例（图层清单 + PNG 图层，总计约 68 KB）
python samples/generate_samples.py

# 2) 跑单个样例
python layer_compositor.py \
    --manifest samples/order/manifest.json \
    --layers   samples/order/layers \
    --out      out/order

# 3) 一键跑全部样例并逐条核对验收标准（17 项，PASS/FAIL）
python verify_samples.py

# 4) 自动化测试（18 个用例）
python -m pytest tests -q
```

`out/<样例>/` 下会生成：

| 文件 | 说明 |
| --- | --- |
| `preview.png` | 合成预览（RGBA，尺寸 = 清单画布尺寸） |
| `crops/<id>.png` | 每层裁剪预览，保留透明通道，文件名以层 id 开头 |
| `report.json` | 逐层核对报告（外接矩形、有效像素、混合模式、透明度等） |

## 3. 输入格式

图层清单 `manifest.json`：

```json
{
  "canvasWidth": 80,
  "canvasHeight": 80,
  "layers": [
    { "id": "b", "order": 1, "blendMode": "normal",   "opacity": 100, "align": "center" },
    { "id": "a", "order": 2, "blendMode": "multiply", "opacity": 100, "align": "center" },
    { "id": "c", "order": 3, "blendMode": "screen",   "opacity": 150, "x": 10, "y": 10 }
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `canvasWidth` / `canvasHeight` | 是 | 输出画布尺寸，正整数 |
| `layers[].id` | 是 | 图层标识，同时是 PNG 文件名（`<id>.png`）与裁剪文件名前缀 |
| `layers[].order` | 是 | 叠放顺序，**升序**叠放（小在下、大在上） |
| `layers[].blendMode` | 否 | `normal`（默认）/ `multiply`；未知值按 `normal` 处理并在报告标注 |
| `layers[].opacity` | 否 | 整数 0–100（默认 100），越界钳制并在报告记录钳制前后值 |
| `layers[].x` / `y` | 否 | 相对偏移（像素），与 `align` 叠加 |
| `layers[].align` | 否 | `left`/`right`/`center`/`top`/`bottom`，可两词组合（如 `"center bottom"`） |
| `layers[].file` | 否 | 自定义 PNG 文件名；只允许层目录内的纯文件名（拒绝路径穿越） |

图层 PNG 只按文件名 `<id>.png`（或 `file` 指定）去 `--layers` 目录查找，
文件缺失或无法解码时该层跳过，其余层照常合成。

## 4. 报告结构（`report.json`）

固定键名（与任务验收对应）：

```json
{
  "canvasWidth": 80,
  "canvasHeight": 80,
  "totalLayers": 3,
  "usedLayers": 3,
  "unavailableLayers": [],
  "duplicateIds": [],
  "duplicates": [],
  "ignoredDuplicates": [],
  "layers": [ ... ]
}
```

- `totalLayers`：清单条目总数（含重复 id 与不可用层）。
- `usedLayers`：实际参与合成的层数。
- `unavailableLayers`：缺失/解码失败层的 id。
- `duplicateIds`：出现重复 id 的 id 列表；细节见 `duplicates`
  （`keptOrder` / `ignoredOrders`）和 `ignoredDuplicates`（`id` / `order`）。
- `invalidEntries`：清单中无法解析（非对象、缺 `id`）而被忽略的条目数，细节见 `warnings`。
- `layers`：按合成顺序（`order` 升序）逐层排列，重复 id 中被忽略的条目不进入此列表。

每层固定字段：

| 字段 | 说明 |
| --- | --- |
| `id` / `order` / `status` | 层 id、叠放序号、`used`/`unavailable` |
| `blendMode` | 实际使用的模式 |
| `blendModeRequested` / `blendModeFallback` | 清单请求值与是否回退到 normal |
| `opacity` / `opacityRequested` / `opacityClamped` | 生效值、原值、是否被钳制 |
| `bbox` | 图层文件内可见像素外接矩形 `[l,t,r,b]`（右下开区间；全透明为 `null`） |
| `canvasBBox` | 同一矩形换算到画布坐标并裁到画布内，供定位返工范围 |
| `visiblePixels` | 图层文件内 alpha>0 的像素数 |
| `contributedPixels` | 真正落到画布上的 alpha>0 像素数（越界裁剪后） |
| `fullyTransparent` | 是否完全透明 |
| `clipped` | 是否被画布裁剪 |
| `position` / `size` | 图层放置坐标与原始尺寸 |
| `cropFile` | 相对输出目录的裁剪文件路径 |
| `notes` | 钳制/回退/跳过等说明，直接给美术看 |

## 5. 行为约定

1. **叠放顺序**：按 `order` 升序逐层 source-over；报告 `layers` 同序。
2. **混合模式**：`normal` 与 `multiply`；其余值（含缺失）按 `normal`，报告标 `blendModeFallback: true`。
3. **不透明度**：钳制到 `[0, 100]`，`150 → 100`、`-10 → 0`（0 时该层视觉不可见但仍生成裁剪图）。
4. **画布与裁剪**：画布取清单尺寸；`align: "center"` 按 `floor((canvas - layer)/2 + 0.5)`
   放置（四舍五入，避免奇数差时偏向一侧），超界部分直接裁掉，不中断运行。
5. **裁剪预览**：只包含本层放到画布后的可见区域（越界已裁），保留 alpha；
   完全透明或完全落在画布外的层写 1×1 全透明 PNG，保证"每层一个文件"。
6. **逐层指标**：`bbox`/`visiblePixels`/`fullyTransparent`/`clipped` 固定字段。
7. **不可用层**：缺失或解码失败 → 跳过、`status: "unavailable"`、记入 `unavailableLayers`，预览不含该层。
8. **重复 id**：只保留 `order` 最小者参与合成（同 order 时取清单中靠前者），
   其余记录进 `ignoredDuplicates`。
9. 每次运行会清理 `crops/` 中上一次遗留的 `.png`，保证裁剪文件与本轮图层一一对应。

## 6. 样例与验收对照

| 样例目录 | 覆盖要求 | 关键期望 |
| --- | --- | --- |
| `samples/order` | 1 | 清单 c,b,a → 报告 b,a,c；预览中 a 盖 b、c 盖 a |
| `samples/blend` | 2 | multiply 手算一致；`screen` 回退 normal 且报告标注 |
| `samples/opacity` | 3 | `150→100`、`-10→0`，报告含钳制前后值 |
| `samples/clip` | 4 | 250×250 居中到 200×200，四边各裁 25px，输出仍 200×200 |
| `samples/metrics` | 5、6 | 每层一个 RGBA 裁剪图；四字段数值与像素一致 |
| `samples/missing` | 7 | 删去 `two.png` 后其余层正常，报告 `unavailableLayers: ["two"]` |
| `samples/corrupt` | 7 | `broken.png` 内容非法，跳过后其余层正常 |
| `samples/duplicate` | 8、9 | `dup` 仅 order=1 参与，报告列出被忽略 order=2；汇总四字段核对 |

`python verify_samples.py` 的实际输出（本机 Windows 11 / Python 3.10.20 / Pillow 12.3.0）：

```
[PASS] 1. report order follows manifest order (b, a, c)                       (['b', 'a', 'c'])
[PASS] 1. preview: a covers b and c covers a                                  (a@(22,22)=(220, 40, 40, 255), c@(40,40)=(240, 200, 30, 255))
[PASS] 2. multiply pixel matches hand calculation                             (got (100, 120, 15, 255), expected (100, 120, 15, 255))
[PASS] 2. unknown mode falls back to normal and is reported                   (blendMode=normal, requested='screen')
[PASS] 2. unknown-mode layer composites as normal                             ((10, 200, 120, 255))
[PASS] 3. 150 clamped to 100 and reported                                     (150 -> 100, notes=['opacity clamped 150 -> 100'])
[PASS] 3. -10 clamped to 0 and layer invisible                                (-10 -> 0, contributed=0)
[PASS] 4. 200x200 canvas kept for a 250x250 centred layer                     (canvas=200x200, layer=[250, 250], position=[-25, -25])
[PASS] 4. all four edges cropped symmetrically                                (canvasBBox=[0, 0, 200, 200], position=[-25, -25], clipped=True)
[PASS] 5. one RGBA crop per used layer, named by id                           (corner.png, empty.png, opaque.png, partial.png)
[PASS] 6. all four metric fields present on every layer
[PASS] 6. metric values match sample pixels                                   (opaque=342, empty=0, partial=128)
[PASS] 7. missing layer marked unavailable, others composited                 (unavailable=['two'])
[PASS] 7. undecodable PNG marked unavailable                                  (unavailable=['broken'])
[PASS] 8. duplicate id: only order=1 used, order=2 ignored and listed         (duplicates=[{'id': 'dup', 'keptOrder': 1, 'ignoredOrders': [2]}])
[PASS] 9. summary keys totalLayers/usedLayers/unavailableLayers/duplicateIds  (total=3, used=2, unavailable=[], duplicateIds=['dup'])
[PASS] 9. unavailableLayers lists every unavailable id                        (total=3, used=2, unavailable=['two'])

17/17 checks passed; outputs in .../out
```

20 个 pytest 用例同样全部通过（`20 passed`），覆盖叠放序、混合、透明度、裁剪、裁剪图、
指标、缺失/损坏、重复 id、汇总字段、CLI 与路径穿越拒绝。

## 7. 文件布局

```
layer_compositor.py        # 合成器 + CLI（唯一运行依赖 Pillow）
samples/generate_samples.py# 确定性样例生成（固定种子，可重复）
samples/<name>/manifest.json + layers/*.png
tests/test_compositor.py   # pytest 端到端用例
verify_samples.py          # 一键跑样例 + 打印验收结果
out/<name>/                # 运行产物（可删，已在 .gitignore）
```

## 8. 已知边界

- 像素运算为 8 位；向量化路径（Pillow C 实现）与参考逐像素实现误差 ≤ 1/255。
- multiply 按 W3C 公式对半透明画布也成立；若美术工具使用预乘 alpha 显示，
  查看时要确认查看器按直通 alpha 解码。
- 超界图层整层仍会参与合成（被裁掉的部分不影响结果），报告用 `clipped` 与
  `contributedPixels` 标出裁剪量，便于判断是否要调整构图。
