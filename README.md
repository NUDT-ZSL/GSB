# 会议录像叠加字幕排版本地核对工具（subtitle-audit）

本地离线运行，读取字幕/标签排版清单（JSON），用 **Pillow FreeType 真实字体度量**
计算每个文字项渲染后的包围盒，判断是否：

1. 越出内边距安全区（**边界**，预览红框）；
2. 与人脸矩形相交/包含（**人脸**，预览黄框，记录人脸编号）；
3. 与其他文字包围盒相交/包含（**文字**，预览紫框，同一对只记一次）。

多类冲突并存时，预览框颜色按 `边界 > 人脸 > 文字` 取最高优先级，报告仍列出全部冲突。
全部在本地完成，不依赖容器、不访问网络。

## 1. 环境与安装

- Windows 11，Python 3.10（实测 3.10.20）
- 依赖：Pillow 9.5.0（精确版本）
- 字体：使用 Windows 自带字体，无需下载
  - 中文：微软雅黑 `msyh.ttc`（粗体 `msyhbd.ttc`）、黑体 `simhei.ttf`、宋体 `simsun.ttc`
  - 英文：Arial `arial.ttf`（粗体 `arialbd.ttf`）、Calibri

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 启动说明

```powershell
# 第一步：生成确定性业务样例清单（无随机数，重复运行逐字节一致）
.\.venv\Scripts\python.exe scripts\generate_sample.py samples\manifest.json

# 第二步：执行核对（输出目录不存在会自动创建）
.\.venv\Scripts\python.exe -m subtitle_audit samples\manifest.json output
```

产物：

| 文件 | 说明 |
| --- | --- |
| `output/preview_<序号>_<id>.png` | 每张有效画布一张预览图，画布原尺寸、深灰背景、安全区虚线、冲突框 |
| `output/audit_report.json` | 核对报告，条目顺序与清单一致 |

一键演示（含坏清单、只读目录等异常场景）：

```powershell
powershell -ExecutionPolicy Bypass -File run_demo.ps1
```

自动验证（19 项断言，覆盖需求 1–13 的全部预期，含像素级框色检查）：

```powershell
.\.venv\Scripts\python.exe scripts\verify.py
```

退出码：`0` 成功；`2` 清单文件不存在/非法 JSON（不生成任何输出）；`3` 输出目录不可写。

## 3. 清单结构

```json
{
  "manifest_id": "demo",
  "sheets": [
    {
      "id": "meeting_main",
      "width": 1280,
      "height": 720,
      "padding": 20,
      "faces": [
        {"id": "face-1", "x": 300, "y": 120, "width": 180, "height": 200}
      ],
      "texts": [
        {
          "id": "t1",
          "text": "会议议程 · 2026 季度复盘",
          "x": 40,
          "y": 40,
          "font_size": 32,
          "font_weight": "bold",
          "font_family": "microsoft_yahei",
          "stroke_width": 2,
          "foreground": "#FFFFFF",
          "stroke_color": "#000000"
        }
      ]
    }
  ]
}
```

字段规则与默认值：

- 画布：`width/height` 必须为正数；`padding` 非负且安全区面积为正，否则整张画布跳过并写明原因。
- 人脸：`x/y/width/height` 为数值、宽高为正且不越出画布；非法人脸忽略并在 `warnings` 说明，不影响其他处理。
- 文字：
  - `font_family` 可选 `microsoft_yahei / simhei / simsun / arial / calibri`，默认 `microsoft_yahei`；也可传本机 `.ttf/.ttc/.otf` 绝对路径。
  - `font_weight` 可选 `regular / bold`，默认 `regular`。
  - `foreground` 默认 `#FFFFFF`，`stroke_color` 默认 `#000000`，`stroke_width` 默认 `2`；颜色值非法回退默认并告警。
  - 位置 `x/y` 为渲染锚点（左上 ascender 锚点 `la`），与度量使用同一锚点，保证包围盒与渲染严格对齐。
- 跳过规则（`skipped=true`、`conflicts=[]`、`error` 非空，且预览图不出现该文字）：
  - 文本为空、仅空白、长度超过 200 字符；
  - 字号小于 6 像素、字号非数值；
  - 字体无法加载（未知字体族/字体文件缺失）；
  - 位置 `x/y` 缺失或非数值。

## 4. 真实字体度量（不做估算）

`subtitle_audit/fonts.py` 通过 `ImageFont.truetype` 加载真实字体，
用 `FreeTypeFont.getbbox(text, stroke_width=..., anchor="la")` 取含描边的真实墨迹包围盒，
渲染时 `ImageDraw.text(..., anchor="la")` 使用同一锚点，两者像素级一致。

实测（Arial “Subtitle 2026”，不含描边宽度）：

| 样式 | 包围盒宽度 |
| --- | --- |
| 20px 常规 | 117 |
| 20px 粗体 | 123（≥ 常规） |
| 40px 常规 | 234（约为 20px 的 2 倍） |

## 5. 需求 1–13 与实际运行结果对照

自动验证输出：**共 19 项核对，通过 19，失败 0。**

| 需求 | 预期 | 实测结果 |
| --- | --- | --- |
| 1 | 负宽画布记录错误，其他条目正常 | `bad_canvas` 跳过，错误“画布宽高必须为正数，实际 width=-1280, height=720”；另两张画布正常出图 |
| 2 | 字号翻倍宽度增大、粗体不窄于常规 | 117 → 234；粗体 123 ≥ 117（中文雅黑 104 = 104） |
| 3 | 贴边（padding=20）文字红框+“边界” | `t2_edge`（left=6 < 20）红框，冲突 `[{"type":"边界"}]` |
| 4 | 压人脸黄框+人脸编号 | `t3_face` 黄框，`{"type":"人脸","face_id":"face-1"}` |
| 5 | 文字相交双方紫框、同一对仅一条 | 仅一对 `t4_overlap_a ↔ t5_overlap_b`，双方各一条“文字”记录 |
| 6 | 多冲突红框优先、报告列全部 | `t6_both` 预览红框，报告同时含“边界”“人脸” |
| 7 | 画布尺寸/深灰角点/安全区虚线 | PNG 1280×720，角点 `(48,48,48)=#303030`，虚线像素可见 |
| 8 | 报告含 id/text/bbox/conflicts/skipped/error | 所有条目字段齐全，bbox 为 `left/top/right/bottom` |
| 9 | 空/空白/超长跳过且不参与冲突 | `t7/t8/t9` 均 skipped、error 非空、预览无该文字 |
| 10 | 顺序与清单一致、冲突不重复 | 顺序一致；冲突按 (类型, 人脸/对方 id) 去重 |
| 11 | 坏清单明确报错、非零退出、无新文件 | 两种场景退出码均为 2，输出目录未创建 |
| 12 | 字号 5/坏字体跳过，其他正常 | `t10`“字号 5 小于最小允许值 6 像素”、`t11`“未知字体族…”，其余文字正常 |
| 13 | 目录自动创建；只读目录报错非零退出 | 新目录自动创建；ACL 拒绝写入时退出码 3 并提示“输出目录不可写” |

> 实现备注：输出目录可写性探测不使用 `tempfile.mkstemp` —— CPython 在 Windows 上会把
> `PermissionError` 当作文件名冲突重试，只读目录下会长时间挂起；本工具改用
> `os.open(..., O_CREAT|O_EXCL)` 直接探测，权限不足立即报错。

## 6. 目录结构

```
project108/
├─ subtitle_audit/
│  ├─ __init__.py
│  ├─ __main__.py        # CLI：清单加载、目录创建/可写探测、退出码
│  ├─ fonts.py           # 真实字体加载与 FreeType 度量
│  └─ checker.py         # 校验、冲突判定、预览渲染、报告
├─ scripts/
│  ├─ generate_sample.py # 确定性样例清单生成
│  └─ verify.py          # 19 项端到端自动验证
├─ samples/manifest.json # 生成出来的业务样例
├─ output/               # 预览 PNG + audit_report.json
├─ run_demo.ps1          # 一键演示（含异常场景）
├─ requirements.txt
└─ README.md
```
