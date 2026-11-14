# 植被精灵场景合成预览工具

读取美术交付的摆放清单 JSON，按清单顺序把植被精灵（草、花、树叶）合成到透明画布上，
基于精灵 alpha 通道叠加柔和阴影，输出合成预览 PNG 与逐精灵报告 JSON，供人工核对场景观感。

## 环境要求

- Python 3.10 或以上，pip 可用
- 安装依赖：`pip install Pillow`

## 本地启动步骤

```powershell
# 1. 安装依赖
pip install Pillow

# 2. 生成业务样例（精灵图 + 清单，确定性绘制，可重复重建）
python make_sample_assets.py

# 3. 按清单合成，输出到清单指定的 output_dir（样例为 sample/output/）
python sprite_compositor.py sample/manifest.json

# 4. 查看结果
#    sample/output/preview.png  合成预览（透明画布）
#    sample/output/report.json  逐精灵报告
```

退出码：`0` 正常执行（允许部分精灵未完成，详见报告）；`1` 整体拒绝执行
（清单非法 / 画布短边超 4096 / 输出目录不可写）；`2` 命令行参数错误。

## 清单 JSON 格式

```json
{
  "canvas": {"width": 800, "height": 600},
  "output_dir": "output",
  "sprites": [
    {
      "name": "grass_patch",
      "source": "assets/grass.png",
      "rect": {"x": 60, "y": 380, "width": 240, "height": 180},
      "shadow": {"offset_x": 10, "offset_y": 12, "blur_radius": 8}
    }
  ]
}
```

- `source`：精灵源图路径，相对清单文件所在目录（也支持绝对路径）。
- `rect`：摆放矩形，`width`/`height` 须为正整数；源图小于矩形时不放大合成，记为未完成。
- `shadow`：`offset_x`/`offset_y`/`blur_radius` 须为非负整数，偏移量不超过画布短边的四分之一。
- 矩形可部分越出画布，溢出部分自动裁掉，画布内内容保留。

## 报告 JSON 格式

```json
{
  "canvas": {"width": 800, "height": 600},
  "completed_count": 3,
  "total_count": 3,
  "sprites": [
    {"name": "grass_patch", "status": "completed", "preview": "preview.png", "reason": null},
    {"name": "bad_one",    "status": "failed",    "preview": null,         "reason": "源图缺失: ..."}
  ]
}
```

- `sprites` 顺序与清单一致；`preview` 为相对输出目录、正斜杠路径，未完成项为 `null`。
- 预览与报告均原子写出（先写临时文件再替换），重复运行结果逐字节一致，无残留条目。

## 业务样例

`make_sample_assets.py` 生成 `sample/`：

| 精灵 | 源图 | 摆放矩形 | 阴影 |
|---|---|---|---|
| grass_patch | assets/grass.png (240×180) | (60, 380) 240×180 | 偏移 (10,12)，模糊 8 |
| flower_bed | assets/flower.png (200×200) | (380, 320) 200×200 | 偏移 (8,10)，模糊 10 |
| leaves_cluster | assets/leaves.png (280×160) | (620, 60) 280×160 | 偏移 (12,8)，模糊 6 |

其中 `leaves_cluster` 右缘越出 800 宽画布，用于演示越界裁剪。运行后
`sample/output/preview.png` 为 800×600 透明画布合成图，`report.json` 三项全部 `completed`。

## 验证记录

`verify_acceptance.py` 在临时目录中确定性重建全部测试资产并逐条核对需求，
运行方式：`python verify_acceptance.py`（全部通过退出码 0）。

最近一次运行（2026-11-14，Python 3.14.7 + Pillow 12.3.0，Windows 11）：**12/12 通过**

| 核对项 | 内容 | 结果 |
|---|---|---|
| 1 | 3 精灵清单：预览尺寸 == 画布，报告顺序 == 清单顺序 | PASS |
| 2 | 部分越界矩形：仅显示画布内部分，报告已完成 | PASS |
| 3 | 阴影贴合 alpha 轮廓，透明角落 alpha=0 无暗块（实测角点 alpha=0，阴影区 alpha=123） | PASS |
| 4 | 源图缺失：该项未完成并写明原因，其余完成，预览正常生成 | PASS |
| 5 | 源图小于摆放矩形：未完成、不放大、预览对应区域全透明 | PASS |
| 6 | 宽为 0 / 偏移为负：均记未完成，原因为参数非法 | PASS |
| 7 | 画布 5000×5000：整体拒绝并提示超限，输出目录无新增文件 | PASS |
| 8a | 输出目录不存在时自动创建并写出结果 | PASS |
| 8b | 输出路径被同名文件占用（不可创建）：整体报错，无预览文件 | PASS |
| 9 | 单精灵合成阶段抛异常：仅该项未完成，其余正常，完成数正确 | PASS |
| 10 | 报告字段齐全、顺序一致、未完成路径为 null、完成数正确 | PASS |
| 11 | 连续运行两次：报告与预览逐字节一致，输出目录无多余文件 | PASS |

说明：第 8b 项在 Windows 上目录只读属性不阻止写入，故采用"输出路径被同名文件占用"
构造确定性的不可写场景；程序在合成前统一做创建 + 试写探测，失败即整体报错，不产生半成品。

## 文件说明

- `sprite_compositor.py` — 合成主程序（CLI + 可导入的 `run()`）
- `make_sample_assets.py` — 业务样例生成器（确定性绘制，无随机数）
- `verify_acceptance.py` — 验收脚本（仅针对本地临时目录，不访问外部对象）
- `sample/` — 样例清单、精灵图与输出
