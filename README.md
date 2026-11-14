# 精灵图集透明边缘预览工具（sprite_preview）

本地命令行工具：读取一张带 alpha 通道的 PNG 图集与一份 JSON 区域配置，为每个有效区域
生成带**棋盘格透明背景**与 **8px 内边距**的独立预览 PNG，并输出一份 JSON 摘要报告。
用于游戏美术批量检查精灵切割后的透明边缘溢色与半透明瑕疵。

纯本地运行，仅依赖 Python 标准库与 Pillow，不访问网络、不依赖容器。

## 环境要求

- Python 3.10+（开发与验证环境：Python 3.14.7，Windows 11）
- Pillow（`pip install Pillow`，验证版本 12.3.0）

```powershell
pip install Pillow
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `sprite_preview.py` | 命令行工具本体 |
| `make_sample.py` | 生成可复现的业务样例（图集、配置、各类异常输入） |
| `verify.py` | 端到端自动验证，覆盖需求 (1)–(12)，共 47 项断言 |
| `sample_data/` | `make_sample.py` 的产物：样例图集与配置（含异常文件） |
| `sample_output/` | 工具对样例的实际输出（预览 PNG + `report.json`） |

## 启动说明

```powershell
# 1. 生成样例数据（图集 64x48 RGBA + 10 条区域配置 + 4 个异常输入文件）
python make_sample.py

# 2. 运行工具：三个位置参数依次为 图集路径、配置路径、输出目录
python sprite_preview.py sample_data/atlas.png sample_data/regions.json sample_output

# 3. 查看帮助（含三个位置参数说明与示例命令）
python sprite_preview.py --help

# 4. 运行全部自动化检查（47 项断言，覆盖 12 条需求）
python verify.py
```

> 隔离运行建议：处理外部来源数据时使用 `python -I sprite_preview.py ...`，
> 避免当前目录中被植入的同名模块（如 `json.py`）被解释器加载。

## 命令行接口

```
usage: sprite_preview [-h] atlas config output_dir

位置参数：
  atlas       图集路径：必须是含 alpha 通道的 PNG 文件
  config      区域配置路径：JSON 数组，每项含 name/x/y/width/height
  output_dir  预览与报告的输出目录，不存在时自动创建
```

## 区域配置格式

JSON 数组，每项为对象：

```json
{"name": "hero", "x": 2, "y": 2, "width": 16, "height": 16}
```

- `name`：非空字符串，同时决定预览文件名（非法路径字符自动替换为 `_`）。
- `x/y/width/height`：必须为 JSON 整数（布尔值、字符串、小数一律拒绝）。
- 负坐标、非正宽高 → `invalid_coordinates`；越界 → `out_of_bounds`，**均不做裁剪修正**。

## 渲染规则

- 预览尺寸 = 区域宽高各加 16px（四周各 8px 内边距），区域内容左上角位于 `(8,8)`。
- 棋盘格单元 8px，按预览图像素坐标交替浅灰 `0xCCCCCC` 与白 `0xFFFFFF`：
  `(x//8 + y//8)` 为偶数取浅灰，奇数取白。
- 区域内容以 alpha 合成叠加在棋盘格上，半透明像素与棋盘格混合后 alpha=255，
  溢色/半透明瑕疵可直接检视；全透明区域仍生成仅含棋盘格的预览。
- 每次运行先依据上一次报告清理失效预览，重跑后输出文件集合与本次有效区域严格一致。
- 图集文件全程只读，任何情况下都不会被修改。

## 报告状态值（report.json → regions[].status）

| 状态 | 含义 | 是否生成预览 |
| --- | --- | --- |
| `success` | 正常渲染 | 是 |
| `fully_transparent` | 裁剪结果 alpha 全为 0，预览仅含棋盘格 | 是 |
| `config_invalid` | 缺字段 / 字段类型错误（含字符串坐标） | 否 |
| `invalid_coordinates` | 负坐标或宽高非正，不裁剪修正 | 否 |
| `out_of_bounds` | 超出图集边界，不裁剪修正 | 否 |
| `superseded` | 同名区域中先出现者，被后出现者覆盖 | 否 |
| `write_failed` | 预览写入失败（未完成），其他区域继续 | 否 |

报告顶层还包含：图集尺寸、配置条目总数、重复名称列表 `duplicate_names`、
分类计数 `summary`，以及按**配置原始顺序**排列的逐区域状态与原因；成功项含 `preview_file`。

## 需求对照与实际运行结果

`verify.py` 的实际运行结果：**共 47 项检查，失败 0 项，全部通过。**

| 需求 | 验证方式与结果 |
| --- | --- |
| (1) 图集校验 | 损坏 PNG → “文件不是可解析的 PNG 图像”；RGB 图 → “图集不含 alpha 通道（当前模式 RGB）”；均退出码 1、无输出 |
| (2) 配置无效 | `missing_width`（缺 width）、`string_coord`（字符串坐标）标记 `config_invalid` 并跳过，其余正常渲染 |
| (3) 负坐标/越界 | `negative` → `invalid_coordinates`；`out_of_bounds`（x+w=70>64）→ `out_of_bounds`；均无预览文件 |
| (4) 同名覆盖 | 两项 `dup`：首项 `superseded`，仅生成一份 `dup.png`，中心像素为后一项的黄色 `(230,200,40,255)`，报告 `duplicate_names: ["dup"]` |
| (5) 独立预览/重跑清理 | 5 个有效区域各生成 `<名称>.png`；重跑文件集合不变；改用子集配置后旧预览被自动删除 |
| (6) 棋盘格 | 逐像素校验 `ghost.png`：颜色仅 `(204,204,204,255)` 与 `(255,255,255,255)`，按 8px 块交替 |
| (7) 尺寸/内边距 | 16x16 区域 → 预览 32x32；内容左上角 `(8,8)` 为不透明红；内边距为棋盘格 |
| (8) 全透明 | `ghost` 状态 `fully_transparent`，预览颜色集合仅两种棋盘格色，无内容像素 |
| (9) 摘要报告 | 含图集 64x48、10 条区域按原始顺序排列、各含明确状态与原因，成功项含 `preview_file` |
| (10) 目录/写入失败 | 嵌套新目录自动创建；预置同名目录使 `hero.png` 写入失败 → 标记 `write_failed`，其他预览照常；图集 SHA-256 运行前后一致 |
| (11) 配置整体校验 | 截断 JSON → 报行列号；JSON 对象 → “必须是 JSON 数组，实际为 dict”；均退出码 1、无任何输出 |
| (12) CLI/--help | 三个位置参数齐全，含示例 `python sprite_preview.py assets/spritesheet.png regions.json out/previews` |

样例主运行控制台输出：

```
处理完成：共 10 条配置，成功 4，全透明 1，写入失败 0，其他跳过 5。
摘要报告：sample_output\report.json
```

10 条配置的状态分布：`success` ×4（hero、dup、edge、pad_test）、`fully_transparent` ×1（ghost）、
`config_invalid` ×2、`invalid_coordinates` ×1、`out_of_bounds` ×1、`superseded` ×1。
