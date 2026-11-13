# 二维矢量画面渲染 CLI（纯 Python 标准库）

把美术团队的二维矢量场景 JSON 渲染为 **PNG** 或 **SVG**，并产出与图像同主名的
**`.txt` 报告**。仅依赖 Python 3.10+ 标准库（`json`、`zlib`、`struct`、`argparse`、
`pathlib` 等），无第三方包、无网络、无容器，可在本地 Windows 直接运行。

## 1. 目录结构

| 文件 | 作用 |
| --- | --- |
| `vector_render.py` | 渲染器主程序：JSON 校验、PNG 光栅化（自带 PNG 编码器）、SVG 生成、报告生成、CLI 入口 |
| `make_samples.py` | 确定性样本生成器（任务代码角色，固定字面量、无随机源），生成到 `samples/` |
| `run_acceptance.py` | 9 项验收自动化脚本（含最小 PNG 解码器做像素级断言），产物在 `acceptance_out/` |
| `samples/` | 生成的测试 JSON（运行后出现，已被 gitignore） |
| `acceptance_out/`、`demo_out/` | 验收与演示产物（已被 gitignore） |

## 2. 本地启动方式（可复现）

前置：已安装 Python 3.10+，本项目实测版本 **Python 3.10.20（Windows 11）**。

```powershell
# 步骤 1：生成确定性测试样本（每次运行逐字节一致）
python -I make_samples.py

# 步骤 2：运行全部 9 项验收（14 个断言，含 5 个补充边界用例）
python -I run_acceptance.py
# 期望末尾输出：通过 14 / 14，全部验收通过，退出码 0

# 步骤 3：手动渲染（扩展名决定输出格式）
python -I vector_render.py samples/basic.json demo_out/basic.png   # 光栅化 PNG + basic.txt
python -I vector_render.py samples/basic.json demo_out/basic.svg   # 矢量 SVG + basic.txt
```

> `-I`（隔离模式）表示不把当前目录加入 `sys.path`、忽略用户环境变量，
> 防止样本目录中出现同名 `json.py` 等被当作模块加载，符合安全运行要求。

## 3. 输入 JSON 约定

```json
{
  "width": 200,            // 画布宽度，整数，1~4096，必填
  "height": 120,           // 画布高度，整数，1~4096，必填
  "background": "#ffffff", // 背景色，#rgb 或 #rrggbb，必填
  "primitives": [          // 图元有序列表，后绘制者覆盖先绘制者，必填
    {"type": "rect",   "x": 10, "y": 10, "width": 80, "height": 60,
     "color": "#ff0000", "opacity": 1.0, "line_width": null},
    {"type": "circle", "cx": 140, "cy": 60, "radius": 35, "color": "#0000ff"},
    {"type": "line",   "x1": 0, "y1": 110, "x2": 200, "y2": 110,
     "color": "#00aa44", "line_width": 4}
  ]
}
```

图元字段说明：

- 公共可选字段：`opacity`（0~1，默认 1）、`line_width`（≥0）。
- `rect` / `circle`：不带 `line_width` 时为**填充**；带 `line_width > 0` 时为**描边**。
- `line`：按线段到像素中心的距离光栅化，`line_width` 默认 1。
- 颜色仅接受 `#rgb` / `#rrggbb`（大小写不敏感），如 `red`、`rgb(255,0,0)` 均属非法。

## 4. 错误处理两级模型

| 级别 | 触发条件 | 行为 |
| --- | --- | --- |
| 场景级致命错误 | 文件不存在、JSON 非法、缺 `width/height/background/primitives`、宽高越界（≤0 或 >4096）、背景色非法、输出扩展名不是 `.png/.svg`、输出目录创建失败 | stderr 打印**指明字段/文件名的中文错误**，退出码 `1`，**不产生任何图像或报告** |
| 图元级可恢复错误 | 未知 `type`（如 `triangle`）、负/零半径、矩形宽高 ≤0、颜色格式错误、缺字段、透明度越界等 | **跳过该图元**，其余正常渲染；报告记录其**列表索引（从 0 开始）与原因**，退出码 `0` |

输出采用“同目录临时文件 + `os.replace` 原子落位”，写入中途失败会清理临时文件，
不会留下半成品图像或报告。

## 5. 渲染语义

- **顺序叠加**：严格按 `primitives` 顺序绘制，后出现者覆盖前者。
- **alpha 合成**：8-bit 整数合成 `out = (src*a + dst*(255-a)) // 255`，
  红底 `#ff0000` 上叠加 50% 蓝 `#0000ff` 的混合像素精确为 **(127, 0, 128)**，
  与常规图像软件的 8-bit alpha 合成结果一致。
- **SVG**：每个图元依次输出 `rect` / `circle` / `line` 元素，颜色用
  `fill`/`stroke`、线宽用 `stroke-width`、透明度用 `fill-opacity`/`stroke-opacity`
  标准属性表达，顺序与输入完全一致。
- **PNG**：纯 Python 光栅化 + 手写 PNG 编码（8-bit RGB、zlib 压缩、CRC 分块），
  无需 Pillow 等第三方库。

## 6. 业务操作样例

```powershell
# 正常渲染（自动创建不存在的多级输出目录）
python -I vector_render.py samples/alpha.json demo_out/sub/dir/alpha.png
# -> 生成 demo_out/sub/dir/alpha.png 与 alpha.txt

# 查看报告（UTF-8 文本）
Get-Content demo_out/sub/dir/alpha.txt

# 异常演示 1：不支持的输出格式（退出码 1，无图像文件）
python -I vector_render.py samples/basic.json demo_out/x.jpg
# 错误：不支持的输出格式：.jpg（仅支持 .png / .svg）

# 异常演示 2：缺少 width（退出码 1，错误信息含字段名 width）
python -I vector_render.py samples/missing_width.json demo_out/x.png
# 错误：缺少字段：width（画布宽度）

# 异常演示 3：输入文件不存在（退出码 1，信息含文件名）
python -I vector_render.py samples/missing.json demo_out/x.png
# 错误：输入文件不存在：samples/missing.json

# 异常演示 4：图元级跳过（退出码 0，报告记录 4 条跳过明细）
python -I vector_render.py samples/skip.json demo_out/skip.png
```

## 7. 实际验证结果（2026-11-13，Python 3.10.20 / Windows 11）

`python -I run_acceptance.py` 实测 **14/14 全部 PASS**，与 9 项验收的对应关系：

| 验收项 | 实测结论 |
| --- | --- |
| 1. 缺 width | stderr 含 `width`，退出码 1 |
| 2. output.jpg | 提示“不支持的输出格式”，退出码 1，目录下无 jpg 文件 |
| 3. circle + triangle | PNG 圆心像素 = (51,102,204) 即 `#3366cc`；报告记录索引 1~4 及原因，triangle 在索引 1 |
| 4. 50% 蓝叠加红 | 混合区像素精确 = **(127, 0, 128)**，未叠加红区 = (255,0,0) |
| 5. SVG 顺序与属性 | `rect@129 < circle@270 < line@338`，`fill/stroke/stroke-width` 属性值正确 |
| 6. 同名 txt 报告 | `result.png` 同目录生成 `result.txt`，含尺寸、成功数 1、跳过数 4、4 条索引+原因 |
| 7. 多级目录 | `sub/dir/out.png` 目录自动创建并出图；另验证父路径被文件占用时报“无法创建输出目录”、无半成品 |
| 8. width=5000 / height=0 | 分别提示 width/height 越界，退出码 1，png 与 txt 均未生成 |
| 9. 文件缺失 / JSON 非法 | 信息含 `missing.json` / “不是合法 JSON”，退出码 1，无任何输出文件 |
| 附加 | 背景色非法被场景级拦截；SVG 半透明图元输出 `fill-opacity="0.5"` |

另已目视核验 `demo_out/basic.png`（红填充矩形、蓝圆、绿色粗线、黑色描边矩形，
覆盖顺序正确）与 `demo_out/alpha.png`（红框 + 紫色混合中心）。
