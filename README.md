# 三维网格表面效果本地预览工具 (mesh_preview)

美术交付前快速核对 OBJ 网格表面效果的本地工具。输入 OBJ 网格、漫反射纹理 PNG
与视角配置 JSON,输出宽 800 的四视角预览 `preview.png` 与报告 `report.json`。
纯 CPU 软光栅化(NumPy),无需 GPU/容器/网络。

## 环境

- Python 3.10+(本机验证为 3.14.7)
- 依赖:`pip install trimesh Pillow numpy`

## 启动方式

```powershell
# 1. 生成确定性样本(无法线球体 / UV 越界平面 / 退化面片立方体 / 纹理 / 视角配置 / 损坏OBJ)
python make_samples.py

# 2. 运行预览工具
python mesh_preview.py --mesh samples/cube.obj --texture samples/texture.png --views samples/views.json --outdir out/cube

# 3. 一键运行全部 8 项验收检查(18 条断言)
python verify.py
```

参数:`--mesh` OBJ 路径(必需);`--texture` 纹理 PNG(可缺省或指向不存在文件,
触发 missing_or_invalid 路径);`--views` 视角配置 JSON;`--outdir` 输出目录。

退出码:0 = 成功(允许部分视角 error);1 = 网格解析失败(不生成 preview.png);
2 = 视角配置不可读。

## 视角配置格式

```json
{"views": [
  {"name": "front", "azimuth": 0,  "elevation": 10},
  {"name": "side",  "azimuth": 90, "elevation": 10},
  {"name": "top",   "azimuth": 0,  "elevation": 90},
  {"name": "iso",   "azimuth": 45, "elevation": 35}
]}
```

`azimuth`/`elevation` 为角度制数字;非法条目仅使该视角标记为 error,其余视角不受影响。

## 报告字段(report.json)

| 字段 | 含义 |
|---|---|
| `meshStatus` | `ok` / `error`(附 `error` 可读信息,且不输出预览图) |
| `textureStatus` | `ok` / `missing_or_invalid`(全部面片以默认灰色渲染) |
| `degenerateFaces` | 被跳过的退化面片索引(0 基,共线或面积 < 1e-8) |
| `outOfRangeUvFaceCount` / `outOfRangeUvFaces` | UV 越界面片数量与索引(这些面片改用棋盘格纹理) |
| `views[]` | 按配置顺序,每个视角 `rendered` 或 `error`(附 `reason`) |

## 实现要点

- **缺法线**:按位置焊接顶点后累加面积加权面法线,得到平滑顶点法线,
  逐像素插值 Lambert 光照(双面),球面无硬边断层。
- **UV 越界**:面片任一顶点 UV 超出 [0,1] 即整面切换为程序化棋盘格,其余面片保持原纹理。
- **退化面片**:面积 < 1e-8(含共线)不渲染、不参与法线累加,报告按索引列出。
- **视角隔离**:每个视角独立校验与渲染,失败视角在其单元格内标注名称与原因
  (中文使用系统中文字体,自动换行,不越出本区域)。

## 本机验证结果(2026-11-14,Windows 11,Python 3.14.7)

`python verify.py` 输出:共 18 项检查,通过 18,失败 0。要点:

1. 默认立方体 `preview.png` 宽 800,`views` 长度 4,四视角均 rendered。
2. 无法线球体球面内部相邻像素亮度最大差 39.0、99.9% 分位 12.0(0–765 轴),无硬边。
3. UV=1.5 平面右半呈蓝白棋盘格(11973 棋盘蓝像素),左半纹理不变(0),越界计数 2。
4. 退化立方体报告 `degenerateFaces=[12]`,其余 12 面正常渲染。
5. 移除纹理后模型纯灰(物体像素 RGB 通道差 0),`textureStatus=missing_or_invalid`。
6. `broken.obj` 不生成 `preview.png`,`meshStatus=error` 并附可读错误信息。
7. side 视角配置非法时该视角 `error` 并注明原因,其余三视角 rendered 且可见。
8. 四区域均有名称标签,side 区域中文原因文本完整可见、未触及单元格边缘。
