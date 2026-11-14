# 材质逐方向光照预览（material-preview）

按美术交付的清单 JSON，为每个材质的每个**合法**水平光照方向单独渲染一张预览 PNG，
并输出一份记录来源与完成状态的 `report.json`。贴图缺失、尺寸不符、法线含零向量、
角度越界等情况都不会中断整批任务：能渲染的照常产出，未完成的在报告里如实区分。

- 纯本地运行，不使用容器，不访问网络。
- 输入（清单 + 贴图）只读；同一份清单重复运行得到逐字节一致的预览与报告。
- 样例由代码确定性生成，可随时重建。

---

## 1. 本地启动

```powershell
# 1) Python 3.10+（本机验证版本：3.14.7）
python --version

# 2) 安装依赖
python -m pip install -r requirements.txt      # Pillow / numpy / pytest

# 3) 生成确定性样例并跑通全部业务场景（会逐条打印核对结果）
python demo.py

# 4) 跑自动化验收测试（24 项）
python -m pytest -q
```

对真实清单运行：

```powershell
python -m material_preview path\to\manifest.json -o path\to\out
```

退出码：`0` 运行完成（部分条目未完成属正常，见报告）；`1` 清单语法/结构错误或输出目录
不安全；`2` 未预期异常（带堆栈）。

> 输出目录会先清掉**本程序命名**的旧文件（`report.json`、`NN_<材质>_a<角度>.png`、
> 临时文件），目录里的其他文件原样保留。若输出目录与输入重叠（例如直接指向
> 贴图目录），程序会拒绝运行以免误删输入。

---

## 2. 清单格式

```json
{
  "schema": "material-preview-manifest/v1",
  "root": ".",
  "light": { "angle_min": 0, "angle_max": 180 },
  "neutral_color": [128, 128, 255],
  "materials": [
    {
      "name": "marble",
      "base_color": "textures/marble_basecolor.png",
      "normal_map": "textures/marble_normal.png",
      "light_angles": [0, 90],
      "width": 32,
      "height": 32
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `root` | 贴图相对目录（相对清单所在目录），默认 `.` |
| `light.angle_min` / `angle_max` | 允许的水平光照角度闭区间；`0°` 指向画面右方，`90°` 指向画面上方 |
| `neutral_color` | 零向量法线像素的约定中性色，默认 `[128,128,255]` |
| `name` | 材质名，需唯一（不区分大小写，避免在大小写不敏感的文件系统上撞名） |
| `width` / `height` | 声明的贴图尺寸；两张贴图都必须与之一致 |
| `light_angles` | 方向列表；越界、非数值、布尔、`NaN/Inf` 都不渲染 |

文件名规则：`<两位材质序号>_<材质名>_a<角度>.png`，例如 `00_marble_a90.png`。
同一材质重复列出同一角度时，第二张起追加 `-2`、`-3` 后缀，保证每条清单项都有自己的产物。

## 3. 报告格式（`out/report.json`）

```json
{
  "schema": "material-preview-report/v1",
  "manifest": "manifest.json",
  "manifest_sha256": "…",
  "neutral_color": [128, 128, 255],
  "light": { "angle_min": 0, "angle_max": 180 },
  "summary": { "materials": 6, "entries": 12, "completed": 7, "incomplete": 5 },
  "materials": [ { "name": "…", "status": "completed|partial|incomplete",
                   "texture_status": "ok|failed", "reason": null, "…": "…" } ],
  "entries": [ { "index": 0, "material": "marble", "angle": 0,
                 "preview": "00_marble_a0.png", "status": "completed",
                 "reason": null } ]
}
```

`entries` 与清单条目一一对应、顺序一致。`status == "completed"` 的条目一定对应磁盘上
一个非空、可解码的 PNG；`incomplete` 条目一定带 `reason` 且 `preview` 为 `null`。

## 4. 业务样例

`python demo.py` 会重建 7 个场景（全部由 `material_preview/samples.py` 用公式生成，
无随机数、无时间戳）：

| 场景 | 内容 | 预期 |
| --- | --- | --- |
| `basic` | marble/brick/metal 各 2 个方向 | 6 张预览，报告全完成 |
| `bad_angle` | marble 含 `270`（越界）+ `90`（合法） | 只出 `00_marble_a90.png`，越界项记未完成 |
| `missing` | glass 缺法线贴图 | 其余 3 张预览齐全，glass 两个方向记未完成 |
| `size` | fabric 法线 16×16，清单声明 32×32 | 该材质零预览，报告记尺寸原因 |
| `zero_normal` | glass 法线中部 6×6 为零向量 | 该区域输出 `(128,128,255)`，整图正常 |
| `all` | 6 材质 12 方向的混合清单 | 7 完成 / 5 未完成，报告与磁盘一致 |
| `syntax_error` | 缺右括号的清单 | 打印行列错误、退出码 1、输出目录无预览 |

单独生成某个样例：

```powershell
python -m material_preview.samples_cli basic samples\basic
python -m material_preview samples\basic\manifest.json -o out\basic
```

## 5. 验证记录

本机（Windows 11，Python 3.14.7，Pillow 12.3.0，numpy 2.5.2）实测：

```
$ python -m pytest -q
24 passed

$ python demo.py
共 23 项核对，通过 23 项。
```

核对项与验收条款的对应关系：

| 条款 | 核对方式 | 实现/测试 |
| --- | --- | --- |
| #1 逐项渲染不遗漏 | 3 材质 × 2 方向 → 6 张，文件名对应 | `test_every_material_direction_gets_its_own_preview` |
| #2 越界/非数值不渲染 | 一个越界 + 一个合法 → 只出合法那张 | `test_out_of_range_angle_is_skipped_but_siblings_rendered`、`test_non_numeric_and_nan_angles_are_rejected` |
| #3 贴图缺失整材质未完成 | glass 缺法线 → 其余齐全 | `test_missing_normal_map_fails_only_that_material`、`test_missing_base_color_map_also_fails_material` |
| #4 尺寸不符零预览 | fabric 16×16 vs 声明 32×32 | `test_size_mismatch_produces_no_preview_for_material`、`test_size_mismatch_between_two_textures_is_reported` |
| #5 零向量输出中性色 | 零向量区逐像素 `(128,128,255)` | `test_zero_normal_pixels_use_neutral_color` |
| #6 各方向共用贴图数据 | 平面法线：0°/90° 相同，180° 逐像素等于基础色×环境光 | `test_directions_share_same_texture_data`、`test_directions_differ_only_by_lighting` |
| #7 后续失败不损坏先前结果 | 末尾材质缺贴图，先前 PNG 非空可解码 | `test_late_failure_does_not_damage_earlier_previews` |
| #8 报告与磁盘一致 | 条目数、完成项的 `preview` 与磁盘一一对应 | `test_report_matches_disk_state` |
| #9 输入只读 / 重复运行一致 | 两次运行输入哈希不变、输出逐字节一致 | `test_repeat_runs_are_byte_identical_and_inputs_untouched` |
| #10 语法错误非零退出且无残留 | 输出行列信息、退出码 1、目录为空 | `test_syntax_error_cli_reports_and_leaves_no_previews` 等 |

## 6. 目录结构

```
material_preview/
  manifest.py     清单解析、结构校验、角度合法性
  textures.py     贴图读取、尺寸校验、法线归一化与零向量标记
  render.py       单方向 Lambert 渲染（纯函数）
  runner.py       逐材质逐方向调度、原子落盘、报告生成
  samples.py      确定性样例（清单 + 贴图）
  cli.py          命令行入口
tests/test_acceptance.py   24 项端到端验收测试
demo.py           7 个业务场景的演示与核对
```
