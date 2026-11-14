# 九宫格纹理切片与预览工具

读取 UI 美术交付的纹理图集切片清单（JSON），按边距把每张 PNG 切成九块，并按目标宽高渲染九宫格预览，供接入方检查边角变形与拉伸区域。

## 环境要求

- Python 3.10+
- Pillow 9.0+

```powershell
python --version
python -c "import PIL; print(PIL.__version__)"
# 如未安装：
python -m pip install "Pillow>=9.0"
```

## 目录结构

```
ninepatch.py          实现（切片 + 渲染 + 清单处理 + CLI）
generate_samples.py   确定性样本生成器（PNG + 清单 JSON）
verify.py             端到端检查脚本（覆盖全部验收项）
samples/
  manifest.json           默认样本清单（11 项，全部成功）
  broken_manifest.json    异常样本清单（含缺失/损坏图片、负边距、超范围边距）
  textures/*.png          样本纹理
output/               默认运行输出目录（运行后生成，已 gitignore）
work/verify/          verify.py 的产出（已 gitignore）
```

## 清单格式

路径字段相对**清单文件所在目录**解析（与进程工作目录无关）。

```json
{
  "textures": [
    {
      "texture_name": "panel_blue",
      "image_path": "textures/panel_blue.png",
      "left": 16, "right": 16, "top": 16, "bottom": 16,
      "target_width": 240,
      "target_height": 120
    }
  ]
}
```

- 顶层也可以直接是数组；也支持 `{"items": [...]}` 等常见包装键。
- 字段别名：`name`/`image`，以及 `target_width`/`width`（高度同理）。
- 边距必须为非负整数。边距为 0 表示该方向没有切片。

## 运行

```powershell
# 生成/刷新确定性样本（可选，仓库已包含样本）
python generate_samples.py

# 默认样本 -> ./output
python ninepatch.py

# 指定清单与输出目录（目录不存在会自动创建）
python ninepatch.py samples/manifest.json output
python ninepatch.py -i samples/broken_manifest.json -o build\preview

# 结果 JSON 也可单独指定
python ninepatch.py -i samples/manifest.json -o build\preview --result build\result.json
```

参数也可用位置形式：`python ninepatch.py <清单> <输出目录>`。

### 输出

- `<输出目录>/<纹理名>_preview.png`：每个成功项的预览图。
- `<输出目录>/result.json`：按清单顺序排列的结果数组，每项为

```json
{ "texture_name": "panel_blue", "status": "成功", "compressed": false, "failure_reason": null }
```

  失败项为 `"status": "失败", "compressed": null, "failure_reason": "<原因>"`。
- 控制台逐项输出 `已生成: <纹理名>` 或 `跳过: <纹理名> - <原因>`，末尾输出 `完成: 成功X项, 失败Y项`。

## 处理规则

1. **顺序**：结果 JSON 与逐行日志严格保持清单顺序。
2. **切片**：按左/右/上/下边距切 1:1 九块；边距为 0 的方向切片宽/高为 0，切割边界等于清单边距。
3. **预览渲染**：四角按原始像素直接拷贝（不缩放）；上下边只横向拉伸、左右边只纵向拉伸；中心双向拉伸。预览尺寸等于目标宽高。
4. **边距压缩**：当 `目标宽 < left + right` 或 `目标高 < top + bottom` 时，按比例压缩该方向的边距，保证非零边距至少保留 1 像素，使九宫格放入目标尺寸，并在结果中标记 `compressed: true`。边距为 0 的方向保持 0。源图中心区退化（如图片宽度恰等于左右边距之和）时，用最近像素列/行填充，不留空洞。
5. **失败跳过**：图片缺失或无法解码 → `图片读取失败`；边距为负、或左右边距之和超过图宽、上下边距之和超过图高 → `边距非法`。失败项不生成预览，其余项照常处理。重复运行时还会清理同名旧预览，避免残留。
6. 清单文件本身不存在或 JSON 非法时，向 stderr 输出错误并以退出码 2 结束。

## 本地检查结果

`python verify.py` 会重建样本后执行 103 项检查，覆盖清单顺序、切片边界、预览尺寸、四角像素一致性、零边距方向、压缩标记与至少 1 像素、失败原因文案、跳过项不生成预览、输出目录自动创建、相对路径解析、控制台行数与汇总计数等。

实际运行结果（Windows 11，Python 3.10.20，Pillow 12.3.0）：

```
检查完成: 103/103 通过
全部通过，明细输出在 ...\work\verify
```

默认样本运行：

```
已生成: panel_blue
...
已生成: no_downscale_needed
完成: 成功11项, 失败0项
```

异常样本（`samples/broken_manifest.json`）：

```
已生成: panel_blue
跳过: missing_image - 图片读取失败: ...\samples\textures\does_not_exist.png (...)
跳过: negative_left - 边距非法: 左边距不能为负数(-1)
跳过: margin_too_wide - 边距非法: 左右边距之和(200)超过图片宽度(96)
跳过: corrupt_image - 图片读取失败: ...\samples\textures\corrupt.png (cannot identify image file ...)
已生成: zero_all_edges
完成: 成功2项, 失败4项
```

结果 JSON 中 `panel_blue`、`zero_all_edges` 为成功且 `failure_reason` 为 `null`；四个失败项 `compressed` 为 `null`，输出目录中不存在它们的预览图。

## 样本覆盖矩阵

| 样本 | 边界情况 |
| --- | --- |
| `panel_blue` / `panel_green_small_border` / `no_downscale_needed` | 常规九宫格，无需压缩 |
| `button_wide` | 左右与上下边距不同的非对称布局 |
| `icon_transparent` | RGBA 透明纹理（保留 alpha） |
| `edge_zero_left` | 左边距为 0（预览无左侧切片） |
| `edge_zero_top_bottom` | 上下边距为 0 |
| `zero_all_edges` | 四边边距全为 0（整体双向拉伸） |
| `compress_horizontal` / `compress_vertical` | 目标宽/高小于对应边距之和，压缩标记为 true |
| `compress_tiny_target` | 极小目标（4×8），压缩后角区各保留 1 像素 |
| `missing_image` / `corrupt_image` | 图片缺失 / 无法解码 |
| `negative_left` / `margin_too_wide` | 负边距 / 边距超过图宽 |

## 说明与限制

- 运行环境不使用容器；仅访问本地文件，测试对象限定在本仓库样本内。
- 预览图与输入保持相同像素模式：`RGB`/`RGBA`/`L`/`LA` 原样保留，其余模式（如调色板 `P`）转为 `RGBA`。
- 缩放使用双线性重采样；角块为纯拷贝，不经过任何重采样。
