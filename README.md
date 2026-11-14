# 地图瓦片金字塔生成器（tilemap）

把一张大尺寸源图切成多级瓦片（`z_x_y.png`），并输出一份可逐项核对的清单
`manifest.json`。纯 Python + Pillow，无容器、无网络依赖，样例可由代码确定
性重建。

## 环境要求

- Python 3.10+
- Pillow 9.0+（本机验证环境：Python 3.14.7 / Pillow 12.3.0）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "Pillow>=9.0" pytest
```

（也可以 `python -m pip install -e .`，项目已提供 `pyproject.toml`。）

## 本地启动

```powershell
# 1. 生成确定性源图样张（samples/，同样的代码每次输出同一份字节）
python samples\make_sample.py

# 2. 生成瓦片与清单
python -m tilemap samples\world_500x300.png out\demo --tile-size 256

# 3. 从层级 1 开始（层级 0 不落盘，清单标记 skipped）
python -m tilemap samples\world_500x300.png out\from_level1 --start-level 1

# 4. 跑测试与端到端验收
python -m pytest -q
python scripts\verify_demo.py
```

> PowerShell 中可用 `python -m tilemap`；等价入口是 `python -m tilemap.cli`。
> 生成物在 `out/`，样张在 `samples/`，均已在 `.gitignore` 中忽略。

## 命令行

```text
python -m tilemap SOURCE OUTPUT [--tile-size N] [--start-level L]
                                [--manifest PATH] [--max-pixels N] [--quiet]
```

| 参数 | 说明 |
| --- | --- |
| `SOURCE` | 源图（PNG/JPEG/BMP/GIF/WEBP/TIFF） |
| `OUTPUT` | 瓦片输出目录，不存在时创建 |
| `--tile-size` | 瓦片边长，默认 256 |
| `--start-level` | 起始层级，默认 0；更低层级不产出文件、清单标记 `skipped` |
| `--manifest` | 清单路径，默认 `<OUTPUT>/manifest.json` |
| `--max-pixels` | 超大图保护：`-1` 用 Pillow 默认，`0` 关闭，正数为上限 |
| `--quiet` | 不打印逐层进度 |

退出码：`0` 成功（允许个别瓦片失败，见清单）；`1` 源图不可读/格式不支持/
输出目录不可用/清单写入失败；`2` 命令行参数非法。

## 业务样例

以 `samples/world_500x300.png`（500×300，四象限不同色调、跨瓦片河流、全透明
孔洞）为源，瓦片 256：

- 最大层级推导：层级 0 为 500×300，超过 256；层级 1 为 250×150，可放入
  单块瓦片 → `max_level = 1`。
- 层级 0：`cols=2, rows=2`，瓦片 `0_0_0 / 0_1_0 / 0_0_1 / 0_1_1`
  （命名 `z_x_y`，x 为列、y 为行）。
- 层级 1：`cols=1, rows=1`，唯一瓦片 `1_0_0.png`。
- 右下瓦片 `0_1_1.png` 只有 244×44 区域命中源图，其余 alpha 全为 0。
- 全透明源区域（`transparent_half_512x256.png` 左半）对应瓦片仍生成 256×256
  的空 PNG，清单 `status: "empty"`。

清单结构（节选）：

```json
{
  "schema": "tilemap-manifest/1",
  "source": {"path": "samples/world_500x300.png", "format": "PNG",
             "width": 500, "height": 300, "mode": "RGBA",
             "file_size_bytes": 9446},
  "tile_size": 256,
  "tile_pattern": "z_x_y.png",
  "max_level": 1,
  "start_level": 0,
  "summary": {"total": 5, "success": 5, "ok": 5, "empty": 0, "failed": 0},
  "levels": [
    {
      "level": 0, "scale": 1, "cols": 2, "rows": 2,
      "level_width": 500, "level_height": 300,
      "source_region": {"x": 0, "y": 0, "width": 500, "height": 300},
      "skipped": false,
      "total": 4, "success": 4, "ok": 4, "empty": 0, "failed": 0,
      "tiles": [
        {
          "level": 0, "x": 0, "y": 0, "path": "0_0_0.png",
          "status": "ok", "empty": false, "error": null,
          "source_region": {"x": 0, "y": 0, "width": 256, "height": 256},
          "source_covered": {"x": 0, "y": 0, "width": 256, "height": 256}
        }
      ]
    }
  ]
}
```

字段含义：

- `levels[].cols/rows`：该级行列数；`level_width/height`：该级缩放后尺寸。
- `levels[].source_region`：该级对应源图区域（逻辑区域）。
- `tiles[]`：按 `level → y → x` 升序；`source_region` 是该瓦片负责的源图
  区域，`source_covered` 是其中真实落在源图内的部分，其余为透明填充。
- `tiles[].status`：`ok`（有内容）/ `empty`（整块 alpha=0，文件仍生成）/
  `failed`（写盘失败，`error` 非空）。
- `levels[].success = ok + empty`，`total = success + failed`；`skipped`
  层级 `tiles` 为空、`total/success/failed` 均为 0。

## 设计要点

- `core.py` 先完成源图校验（存在性、`Image.verify()`、格式白名单），之后才
  创建输出目录，因此损坏/不支持/缺失的源图不会留下任何瓦片或清单。
- 裁剪严格按本瓦片坐标 `(x*tile, y*tile)` 进行，越界部分以
  `Image.new("RGBA", ..., (0,0,0,0))` 打底，不会串入相邻区域。
- 每块瓦片先写临时文件再 `os.replace` 原子替换：失败不会截断旧瓦片，也不会
  留下半成品；失败仅影响该瓦片，其余继续。
- 清单同样原子写入，UTF-8、`indent=2`、键顺序固定，保证可复现。
- 更高层级使用 LANCZOS 缩放；空瓦片判定基于缩放后图像的 alpha 通道。

## 验证记录

`python scripts\verify_demo.py` 在本机（Windows 11 / Python 3.14.7 /
Pillow 12.3.0）的输出摘要：

```text
[1-geometry] PASS level0=2x2 tiles, top=level1 1x1 tile,
             files=['0_0_0.png','0_0_1.png','0_1_0.png','0_1_1.png','1_0_0.png']
[2-crop-pad] PASS 0_1_1.png size=(256, 256), covered 244x44 px match source=True,
             alpha outside source all 0=True
[2-no-leak]  PASS tiles matching own source region only, bad=[]
[4-manifest] PASS fields=[...], manifest tiles=5, disk pngs=5, problems=[]
[7-stdout]   PASS stdout level0:4/4, level1:1/1
[determinism] PASS 6 files byte-identical across runs
[3-empty]    PASS 0_0_0.png exists size=(256, 256) alpha-empty,
             manifest status=empty, level0 empty count=1
[5-isolated-failure] PASS failed=[('0_1_0.png','failed',
             "cannot write tile .../0_1_0.png: PermissionError: 拒绝访问。")],
             other tiles present=True, level0 total=4 success=3 failed=1
[6-start-level] PASS files=['1_0_0.png'], level0 skipped=True tiles=0 total=0
[8-corrupt]  PASS exit=1, output dir created=False
[8-unsupported] PASS exit=1, output dir created=False
=== verification summary: 12/12 passed ===
```

单元测试：`python -m pytest -q` → `23 passed`。

失败瓦片场景的做法：先把 `out/verify/failure/0_1_0.png` 写成只读文件，
再运行生成器。该瓦片状态为 `failed` 且 `error` 含 `PermissionError`，其余
4 块瓦片照常生成，旧文件内容保持不变，清单 `failed=1`、`success=3`。瓦片写入
走「临时文件 + 原子替换」，替换失败只会影响该瓦片，不会截断已存在的旧文件；
错误信息固定指向最终瓦片路径（不含随机临时文件名），因此两次运行的验证报告
逐字节一致。
（POSIX 下 rename 会覆盖只读文件，因此 `test_read_only_targets_keep_generating`
仅在 Windows 运行；跨平台的等价场景「目标位置被目录占用」由
`test_tile_failure_is_isolated_and_reported` 覆盖。）
