# 浮空文字标签离线渲染工具

在本地离线渲染游戏浮空文字标签：读取标签清单 JSON 与场景背景 PNG，按清单逐条
在指定坐标绘制文字（含全局描边、阴影），合成输出 PNG，并生成逐标签核对报告。
不依赖游戏引擎截图，不访问网络。

## 环境

- Python 3.10+
- Pillow 10.x+（本机实测 12.3.0，接口兼容）
- 中文字体：默认依次回退 `C:\Windows\Fonts\msyh.ttc` → `simhei.ttf` → `arial.ttf`，
  也可在清单 `style.font_path` 中显式指定

## 启动说明

```powershell
# 1. 确定性生成业务样例（背景 PNG + 清单 JSON，固定随机种子，多次生成字节一致）
python tools\gen_sample.py --outdir samples

# 2. 渲染并生成核对报告
python tools\render_labels.py --manifest samples\manifest.json --background samples\background.png --outdir output
```

产物（重复运行覆盖同名文件，不残留旧报告条目）：

- `output/composited.png` — 合成图
- `output/report.json` — UTF-8 JSON 数组，按清单原顺序，每项含
  `id` / `status` / `position` / `overflow_px` / `safe_area_overflow` / `reason_code`

退出码：`0` 成功；`2` 输入错误（清单非法、背景损坏、背景尺寸与清单声明不符），
此时打印可读错误且不生成任何输出文件。

## 清单格式

```json
{
  "background": {"width": 1600, "height": 900},
  "style": {
    "font_path": "C:\\Windows\\Fonts\\msyh.ttc",
    "fill": "#FFFFFF",
    "outline": {"width": 2, "color": "#1A1A1A"},
    "shadow": {"offset": [2, 2], "color": "#000000A0"}
  },
  "safe_area": {"left": 0, "top": 0, "right": 1280, "bottom": 900},
  "labels": [
    {"id": "title", "text": "暮色森林", "x": 100.4, "y": 60, "font_size": 48}
  ]
}
```

## 渲染与报告规则

| 规则 | 行为 |
|---|---|
| 小数坐标 | 四舍五入取整（100.4→100，100.6→101），报告记录实际落点像素 |
| 描边 / 阴影 | 按清单全局配置绘制；越界部分由画布边界裁剪，不中断整批渲染 |
| 坐标越界 | 仍尝试渲染；报告区分 `clipped`（部分可见）与 `invisible`（完全不可见，原因码 `FULLY_OUT_OF_BOUNDS`） |
| 安全区溢出 | 文字仍按原坐标渲染，报告 `safe_area_overflow=true` 及 `overflow_px`（四边超出量最大值，如右缘 1300、安全区右界 1280 → 20） |
| 空文字 / 字号 < 1 | 不渲染，状态 `skipped`，原因码 `EMPTY_TEXT` / `FONT_SIZE_LT_1` |
| 背景损坏 / 尺寸不符 | 打印可读错误，退出码非零，不生成残缺合成图 |

标签状态：`rendered`（完整可见）、`clipped`（部分被裁剪）、`invisible`（完全不可见）、`skipped`（跳过）。

## 样例实测结果

`python tools\gen_sample.py --outdir samples` 后运行渲染，输出：

```
渲染完成: output\composited.png
核对报告: output\report.json (共 10 条: clipped=1, invisible=1, rendered=6, skipped=2)
```

报告要点（完整内容见 `output/report.json`）：

- `title`：x=100.4 → 落点 100；`subtitle`：x=100.6 → 落点 101
- `overflow-right`：右缘越过安全区右界 1280，`overflow_px=71`
- `offscreen`（-50,-50）：`invisible` + `FULLY_OUT_OF_BOUNDS`，工具正常退出
- `clipped-left`（x=-30）：`clipped`
- `empty-text` / `zero-size`：`skipped`，原因码 `EMPTY_TEXT` / `FONT_SIZE_LT_1`

故障注入实测：截断的 PNG → 退出码 2 且输出目录无新文件；清单声明尺寸与背景
实际尺寸不符 → 退出码 2 并提示两边尺寸。连续两次运行（第二次清单条目更少）→
输出文件名一致，第二次报告仅含本次条目。
