# 图层合成预览工具

美术交付图层清单（JSON）与各图层 PNG，本工具按清单顺序把图层叠加到画布上，
输出一张预览图和一份 JSON 报告，用于交付前快速确认混合效果。
已成功合成的图层完整保留；未完成图层在报告中如实列明文件名与原因。

## 环境要求

- Python 3.10 及以上（本机验证：Python 3.14.7）
- Pillow：`pip install Pillow`（本机验证：Pillow 12.3.0）
- 不使用容器，仅本地运行

## 文件说明

| 文件 | 作用 |
|---|---|
| `composite.py` | 合成工具主程序 |
| `make_samples.py` | 生成业务示例清单与 PNG（确定性重建，无随机） |
| `run_checks.py` | 12 项需求核对脚本（确定性验证） |
| `sample/` | 示例清单、图层 PNG、输出的预览图与报告 |

## 本地启动步骤

```powershell
# 1. 安装依赖
pip install Pillow

# 2. 生成业务示例（sample/ 目录：清单 + 各图层 PNG）
python make_samples.py

# 3. 按清单合成预览图并生成报告
python composite.py sample/manifest.json -o sample/preview.png -r sample/report.json

# 4. 运行 12 项需求核对
python run_checks.py
```

清单与输出路径均可自定义：`python composite.py <清单.json> -o <预览图.png> -r <报告.json>`。
图层文件路径相对于清单所在目录解析。

## 清单格式

```json
{
  "width": 800,
  "height": 600,
  "layers": [
    {"file": "bg.png", "mode": "normal", "opacity": 1.0,
     "offsetX": 0, "offsetY": 0, "width": 800, "height": 600}
  ]
}
```

- `width`/`height`：画布宽高，须为正整数；缺失或非法时不生成预览图，报告标记 `configValid: false`。
- 每个图层条目：`file`（文件名）、`mode`（`normal`/`multiply`/`screen`）、
  `opacity`（0~1 浮点）、`offsetX`/`offsetY`（整数，可为负）、`width`/`height`（声明的图层尺寸）。
- 同一文件名可出现多次，每个条目独立处理、独立记录。

## 业务样例

`sample/manifest.json` 共 10 个图层，覆盖正常与异常情形：

| # | 文件 | 说明 | 结果 |
|---|---|---|---|
| 0 | bg.png | 800x600 浅蓝网格底图，normal | 成功 |
| 1 | multiply_circle.png | 红圆，multiply，opacity 0.9 | 成功 |
| 2 | screen_circle.png | 绿圆，screen，opacity 0.8 | 成功 |
| 3 | deco.png | 黄矩形，normal，opacity 0.6 | 成功 |
| 4 | offset_layer.png | 200x200，offsetX=-50，仅右侧 150 列可见 | 成功 |
| 5 | transparent.png | 全透明，计为成功且画面不变 | 成功 |
| 6 | overlay_mode.png | 模式 overlay 不受支持 | 未完成 |
| 7 | bad_opacity.png | opacity 1.5 越界 | 未完成 |
| 8 | mismatch.png | 声明 100x100，实际 50x50 | 未完成 |
| 9 | missing.png | 文件缺失 | 未完成 |

运行结果（`sample/report.json` 摘要）：

```json
{
  "configValid": true,
  "canvas": {"width": 800, "height": 600},
  "totalLayers": 10,
  "successCount": 6,
  "failedCount": 4,
  "errors": [
    {"index": 6, "file": "overlay_mode.png", "reason": "不支持的混合模式: overlay"},
    {"index": 7, "file": "bad_opacity.png", "reason": "不透明度越界(须为0~1): 1.5"},
    {"index": 8, "file": "mismatch.png", "reason": "尺寸不符: 清单声明100x100, 实际50x50"},
    {"index": 9, "file": "missing.png", "reason": "图层文件缺失或无法解码: ..."}
  ]
}
```

预览图 `sample/preview.png` 为 800x600 PNG，仅含 IHDR/IDAT/IEND 块，
无 EXIF 或文本段等元信息；输出文件已存在时以本次结果覆盖。

## 验证记录

`python run_checks.py` 输出（12/12 通过，工作区 `checks_workspace/` 每次运行重建）：

```
[PASS] 核对1: 清单800x600时输出为800x600 — 实际(800, 600)
[PASS] 核对2: overlay模式图层未合成且报告列明
[PASS] 核对3: opacity=1.5时输出与跳过该图层相同
[PASS] 核对4: 宽200偏移X=-50时仅右侧150像素列可见
[PASS] 核对5: 声明100x100实际50x50时报告尺寸不符
[PASS] 核对6: 文件缺失时预览仍生成且报告记录读取失败
[PASS] 核对7: 全透明图层计为成功且预览内容不变
[PASS] 核对8: 输出PNG无EXIF或文本段 — 块序列: ['IHDR', 'IDAT', 'IEND']
[PASS] 核对9: 两图层出错时successCount=总数-2且errors两条
[PASS] 核对10: 缺失width时无PNG输出且报告配置无效
[PASS] 核对11: 同名文件两个条目各自记录错误
[PASS] 核对12: 旧报告被本次结果覆盖无残留
验证结果: 12/12 通过
```
