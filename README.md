# 地形与热力场：规则网格等值线预览与最近等值线定位

把规则网格标量场按指定级别画成等值线预览 PNG，并对每个查询点求出最近的等值线点、
所属级别与欧氏距离。输入是一个本地场景 JSON，输出一张 PNG 和一份结果 JSON。

纯本地运行，不依赖容器或网络；唯一第三方依赖是 Pillow。

---

## 1. 环境与安装

要求 **Python 3.10+**、**Pillow**（10.x 及以上；本机实测 Pillow 12.3.0 / Python 3.14.7）。

```powershell
python --version                 # 3.10+
python -m pip install -r requirements.txt
```

`requirements.txt` 的内容：

```
Pillow>=10.0
pytest>=7.4        # 仅跑测试时需要
```

> 说明：Pillow 10.0–10.4 没有面向 Python 3.14 的预编译轮子，在 3.14 上安装会尝试源码编译
> 并因缺少 zlib 失败（本机实测）。若环境是 Python 3.14，请直接安装 Pillow 11/12；若必须使用
> Pillow 10.x，请配 Python 3.10–3.13。代码本身只用到了 `Image`/`ImageDraw` 的稳定 API，
> Pillow 10–12 均可用。

代码结构：

```
contour_tool/
  scene.py      场景读取与校验（含 NaN/Inf 缺口的识别）
  contour.py    marching squares 等值线提取 + 最近点查询 + 调色
  render.py     Pillow 预览渲染（与定位共用一套世界→像素映射）
  report.py     结果 JSON 组装
  output.py     原子写盘：先写临时文件再改名，失败回滚
  cli.py        命令行入口
  errors.py     业务错误类型
demo.py         一键复现全部验收场景
examples/scenes 示例场景 JSON（basic / terrain / with_gaps / invalid_length）
tests/          pytest 用例（57 个）
```

## 2. 启动与用法

```powershell
# 基本用法
python -m contour_tool.cli examples/scenes/terrain.json `
    --png examples/out/terrain_preview.png `
    --json examples/out/terrain_result.json

# 可选：在预览图上标出查询点（默认不标）
python -m contour_tool.cli examples/scenes/terrain.json `
    --png examples/out/terrain_preview.png `
    --json examples/out/terrain_result.json --markers

# 一键复现全部验收场景（生成 examples/out/demo/，并在终端打印逐项结论）
python demo.py

# 跑测试
python -m pytest -q
```

退出码约定：

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功，PNG 与结果 JSON 均已写出 |
| 2 | 场景文件不合法（字段缺失/类型错误/长度不符等），**不产出任何文件** |
| 3 | 输出失败（目录不可写、路径是文件、写盘中途失败），**不留半成品文件** |

## 3. 场景 JSON 格式

```json
{
  "grid": {
    "width": 9,
    "height": 7,
    "values": [0.0, 0.4, "...（width×height 个，行优先，iy=0 为顶行）"]
  },
  "levels": [7.5, 2.5, 7.5, 0.5, 5.0, 2.5],
  "queries": [
    {"x": 300, "y": 350, "label": "山顶附近"},
    {"x": 100, "y": 200, "label": "左上角"}
  ],
  "canvas": {"width": 800, "height": 600},
  "world": {"min_x": 100, "min_y": 200, "max_x": 500, "max_y": 500},
  "background": [248, 248, 244],
  "line_width": 2
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `grid.width` / `grid.height` | 是 | 正整数；`values` 长度必须恰好为 `width*height` |
| `grid.values` | 是 | 数字数组；允许 `null`、`NaN`、`Infinity`，这些视为“缺口” |
| `levels` | 是 | 非空数字数组；可含小数，重复项自动去重，输出按升序 |
| `queries` | 否 | 数组，元素为 `{x, y, label?}`；越界或非数值的条目会被跳过并记录原因 |
| `canvas` | 是 | 预览图尺寸（像素），1–10000 |
| `world` | 否 | 网格四角对应的世界坐标；缺省为 `x∈[0,width-1]`、`y∈[0,height-1]` |
| `background` | 否 | 背景 RGB，缺省 `[255,255,255]` |
| `line_width` | 否 | 等值线宽（像素），缺省 2 |

坐标约定：格点 `(ix, iy)` 位于世界上 `(min_x + ix*cell_x, min_y + iy*cell_y)`，
行优先且 `iy=0` 是最小 y（即画面顶部）；世界 y 向下增长，与 PNG 像素 y 一致。
预览和定位共用同一个映射：

```
px = (x - min_x) / (max_x - min_x) * canvas.width
py = (y - min_y) / (max_y - min_y) * canvas.height
```

`distance` 是**世界坐标**下的欧氏距离，`pixel` 是同一最近点换算后的像素位置（保留 3 位小数）。

## 4. 结果 JSON 结构

```json
{
  "schema": "grid-contour-result/1",
  "grid": {"width": 9, "height": 7},
  "world": {"min_x": 100.0, "min_y": 200.0, "max_x": 500.0, "max_y": 500.0},
  "canvas": {"width": 800, "height": 600},
  "data_range": {"min": 0.0, "max": 9.5},
  "levels": [
    {"level": 0.5, "in_data_range": true, "empty": false,
     "segment_count": 12, "segments": [{"start": [x1, y1], "end": [x2, y2]}, ...]}
  ],
  "affected_levels": [],
  "queries": [
    {"index": 0, "x": 300, "y": 350, "label": "山顶附近",
     "status": "hit", "level": 7.5,
     "nearest": [286.436304, 315.302172], "distance": 37.254706,
     "pixel": [372.873, 230.604]}
  ],
  "summary": {"level_count": 4, "empty_level_count": 0, "query_count": 3,
              "hit_count": 3, "miss_count": 0, "skipped_count": 0},
  "warnings": []
}
```

查询条目的 `status`：

* `hit` —— 命中，`level` / `nearest` / `distance` / `pixel` 均有值；
* `miss` —— 所有级别都没有可用线段（例如级别全部超出数据范围），
  **`nearest` 为 `null` 而不是 `[0, 0]`**，`level` 与 `distance` 同为 `null`；
* `skipped` —— 坐标越界、非数值或条目结构非法；`reason` 给出可读原因，其余字段为 `null`。

`affected_levels` 列出因缺口（NaN/Inf/null）而可能缺失线段的级别，其余级别照常生成。

## 5. 验收场景与实际运行结果

以下为 `python demo.py` 的实测输出（完整记录见 `examples/out/demo_run.txt`，
生成物在 `examples/out/demo/<阶段>/`）。

| # | 场景要求 | 实测结论 |
| --- | --- | --- |
| 1 | 3×2、6 个值可载入；长度不符报错且不产出预览 | `3×2 网格 6 个值：exit=0，预览=True，级别=1`；长度不符：`exit=2，预览=False，结果=False，错误=场景错误：标量数组长度不符：网格 3×2 需要 6 个元素，实际为 5 个` |
| 2 | 两端 0/10、级别 5 时端点落在格边中点 | 单行：端点 `[5.0, 0.0] → [5.0, 0.0]`；单格（角点 0/0/10/10）：`[2.0, 1.0] → [0.0, 1.0]`，正是两条格边中点 |
| 3 | PNG 存在，级别颜色不同，空白为背景色 | `PNG=preview.png，尺寸=(120, 120)，等值线颜色=[(31, 119, 180), (214, 39, 40)]，左上角背景=(240, 240, 240)` |
| 4 | 点落在线段上时距离≈0 且命中该级别，条数与查询数一致 | `状态=hit，级别=5.0，距离=0.0，最近点=[1.0, 0.0]` |
| 5 | 级别升序去重，可含小数 | 输入 `[7.5, 2.5, 7.5, 0.5, 2.5]` → `输出级别： [0.5, 2.5, 7.5]` |
| 6 | NaN 格跳过相关线段，结果标明受影响级别，其余照常 | `受影响级别： [5.0] ；各段数： [(5.0, 0), (25.0, 1)]`，进程 exit=0 |
| 7 | 级别超出数据范围时为空结果而非报错 | `级别 5.0: empty=False, in_data_range=True, segments=1`；`级别 15.0: empty=True, in_data_range=False, segments=0` |
| 8 | 越界/非数值查询点跳过并记录原因，后续点正常 | `[0] 越界: status=skipped ... reason=查询点 (9, 0) 超出世界范围 x[0, 2] y[0, 2]`；`[1] 非数值: skipped`；`[2] 合法: hit level=5.0 distance=0.0` |
| 9 | 输出不可写时无残留文件、错误可识别 | 输出路径是文件：`exit=3，错误=输出位置 ... 已存在且不是目录...`；ACL 只读目录：`exit=3，错误=输出目录 ... 不可写（Permission denied），请检查目录权限，残留=[]` |
| 10 | 预览与定位共用映射，像素偏差 ≤1 | `最近点世界坐标=[114.0, 158.0]，像素=[152.0, 210.667]，±1 像素内命中线段像素=6 个` |
| 11 | 未命中时最近点 null；命中条目字段齐全 | `status=miss，level=None，nearest=None，distance=None` |

业务样例：

```
terrain: exit=0，级别=[0.5, 2.5, 5.0, 7.5]
  山顶附近: status=hit level=7.5 nearest=[286.436304, 315.302172] distance=37.254706 pixel=[372.873, 230.604]
  坡脚:     status=hit level=0.5 nearest=[466.875, 476.875] distance=9.722718 pixel=[733.75, 553.75]
  左上角:   status=hit level=0.5 nearest=[128.125, 228.125] distance=39.774756 pixel=[56.25, 56.25]

with_gaps: exit=0，级别=[3.5, 6.5, 15.0]
  缺口附近: status=hit level=3.5 nearest=[83.783784, 202.702703] distance=16.439899 pixel=[134.054, 324.324]
  越界点:   status=skipped  reason=查询点 (400, -10) 超出世界范围 x[0, 300] y[0, 300]
  非法点:   status=skipped  reason=坐标缺失或不是有限数字
  有效点:   status=hit level=6.5 nearest=[189.726027, 272.60274] distance=64.372631 pixel=[303.562, 436.164]
```

预览示例（`examples/out/demo/terrain_preview.png`）：中央山丘产生 4 条闭合/半闭合等值线，
级别 0.5 / 2.5 / 5.0 / 7.5 分别用红、蓝、绿、橙四色绘制，空白处保持背景色 `(248,248,244)`。

## 6. 实现要点

* **等值线提取**：对每个网格单元做 marching squares。四条格边分别判断两端是否跨越级别，
  跨越时按 `t = (v_a - level) / (v_a - v_b)` 线性插值出交点；只有 2 个交点时出一条线段，
  鞍点（4 个交点）用单元中心值决定连通方式，退化情形按角度排序配对。
  两端恰好都等于级别的格边被忽略，避免产生无意义的重合线。
* **缺口处理**：只要单元四角含 NaN/Inf/null，该单元直接跳过；数据范围 `data_range`
  只统计有效值。`affected_levels` 通过“缺口单元中有效角点的取值区间”判定。
* **级别去重与排序**：读入时 `sorted(set(values))`，渲染配色和结果 JSON 都按该顺序。
* **最近点**：对每个查询点遍历所有级别的所有线段，做点到线段投影（参数 `t` 夹在 `[0,1]`），
  取最小世界距离；等距时保留先遇到的较低级别。
* **原子写盘**：两个目标目录先做可写探测；已存在的旧 PNG 先改名备份，
  然后写临时文件 → `fsync` → `os.replace`。任一环节失败都删除本轮产物并恢复旧文件。
* **错误信息**：所有 `SceneError` / `OutputError` 都是面向业务人员的中文描述，CLI 分别以退出码 2/3 返回。

## 7. 测试

```powershell
python -m pytest -q
# 57 passed
```

覆盖：场景校验（缺字段、类型错误、长度不符、非法背景色、非法线宽、顶层非对象、
非有限级别/世界坐标等 13 组参数化用例）、中点插值、鞍点配对、单行/单列网格、
NaN/Inf 缺口、级别去重排序、越界查询、未命中 null、只读目录、
PNG 写入失败与 JSON 写入失败的回滚、像素映射一致性等。
`tests/conftest.py` 提供场景构造辅助函数。
