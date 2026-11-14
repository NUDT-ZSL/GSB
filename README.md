# 离线纹理图集打包预览器

在接入引擎前，离线确认小图排布与透明边缘出血效果。输入图集配置 JSON 与本地小图目录，输出图集 PNG 与落位清单 JSON。全程本地运行，不访问网络。

## 环境

- Python 3.10 及以上（本机验证：Python 3.14.7）
- Pillow 10.x 及以上（本机验证：Pillow 12.3.0）

## 文件

| 文件 | 说明 |
|---|---|
| `atlas_packer.py` | 打包器主程序（CLI） |
| `generate_samples.py` | 确定性生成样例小图与配置（无随机因素） |
| `verify.py` | 验收脚本，逐条验证需求 1–10 |
| `sample_data/` | 生成的样例（sprites/ 与 4 份配置） |

## 启动方式

```powershell
# 1. 生成确定性样例（sprites/*.png 与 config*.json）
python generate_samples.py sample_data

# 2. 打包：指定配置路径与输出目录
python atlas_packer.py --config sample_data\config.json --out out

# 产物：out\atlas.png（图集）、out\manifest.json（落位清单）

# 3. 一键验收（重新生成样例并验证全部 10 条需求）
python verify.py
```

参数缺失时程序输出用法说明并以非零码退出（argparse，退出码 2）。

## 配置格式

```json
{
  "canvas": {"width": 128, "height": 128},
  "bleed": 4,
  "sprites": [
    {"name": "red",   "src": "sprites/red.png"},
    {"name": "green", "src": "sprites/green.png", "rotate": true}
  ]
}
```

- `canvas.width/height`：正整数，非法时退出码 2 且不产生任何输出文件。
- `bleed`：非负整数，出血像素数，缺省 0。
- `sprites[].src`：相对配置文件所在目录解析；`rotate` 为 `true` 时顺时针旋转 90°。

## 行为说明

- **排布**：按配置顺序货架式排布，占位足迹 = 落位矩形 + 四边出血，因此出血区既不超出画布也不侵占相邻落位矩形；放不下的标记 `overflow`，不写入图集。
- **出血**：复制小图边缘像素（含 alpha）向外扩展，透明像素保持透明。
- **容错**：源 PNG 缺失或无法解码标记 `missing_source`，其余小图继续打包。
- **确定性**：同一配置重复运行，atlas.png 与 manifest.json 逐字节一致。
- **原子写入**：先写临时文件再替换；输出目录不可写时退出码 3 并提示，不留残缺文件，不修改源文件。

## 清单格式（manifest.json）

```json
{
  "canvas": {"width": 128, "height": 128},
  "bleed": 4,
  "sprites": [
    {
      "name": "green",
      "src": "sprites/green.png",
      "rotated": true,
      "rect": {"x": 44, "y": 4, "width": 40, "height": 24},
      "has_alpha": false,
      "status": "placed"
    }
  ]
}
```

`status` 取值：`placed`（已落位）、`missing_source`（源缺失/无法解码）、`overflow`（尺寸溢出）；未处理项 `rect` 为 `null`。旋转项 `rect` 宽高按旋转后尺寸记录。

## 实际验证结果（本机，Windows 11）

`python verify.py` 输出：**24/24 项通过**，覆盖：

1. 小画布下 `big` 不写入图集（图集无其紫色像素），清单 `rect=null, status=overflow`；
2. bleed=4 时各落位矩形外扩区颜色与边缘一致，相邻矩形内部像素与源图逐字节一致；
3. 24×40 的 `green` 旋转后清单记录 40×24，图集中亮绿标记边位于右侧（顺时针 90° 方向正确）；
4. 缺失源 `ghost` 标记 `missing_source`，`red`/`blue` 正常落位并出现在图集中；
5. 半透明 `fade` 角像素 alpha=0、中心 alpha=255 均保留，出血区 alpha=0 未被填实；
6. 清单字段齐全，`missing_source` 与 `overflow` 可区分，画布尺寸与配置一致；
7. 缺 `canvas` 字段的配置退出码 2，报错指明问题，输出目录无图集 PNG；
8. 连续两次运行图集 PNG 哈希相同、清单文本相同；
9. 输出目录 ACL 只读时退出码 3 并提示写入失败，源文件哈希不变；
10. 无参数退出码 2 并输出用法说明；合法参数生成图集与清单。
