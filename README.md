# 光照贴图接缝漏光检出（Lightmap Seam Leak Checker）

烘焙前给场景美术用的本地工具：读取烘焙设定 JSON、光照贴图 PNG 与每张贴图的
第二套 UV 接缝清单 CSV，逐条接缝比较两侧光照亮度，输出**逐贴图标注图**
（`<贴图名>_seam_report.png`）与**检出结果 JSON**（`seam_result.json`）。

纯本地运行，无网络、无容器、无外部服务。

---

## 1. 环境与启动

要求 Python 3.10+ 与 Pillow。

```powershell
# Windows PowerShell（在本仓库根目录）
python -m pip install -r requirements.txt

# 1) 生成确定性样例（已提交在 samples/，可重复生成，字节级一致）
python tools/generate_samples.py

# 2) 运行检出
python -m lightmap_seams --settings samples/bake_settings.json --output out/sample

# 或者等价的封装脚本
python tools/run_check.py --settings samples/bake_settings.json --output out/sample
```

输出目录会得到：

```
out/sample/
  seam_result.json
  sample_lightmap_a_seam_report.png
  sample_lightmap_b_seam_report.png
  sample_lightmap_c_seam_report.png
```

### 验证（全部本地可复现）

```powershell
python -m unittest discover -s tests -v   # 25 个用例，逐条对应验收项
python tools/acceptance_check.py          # 打印 17 项逐项 PASS/FAIL 与实测值
```

`acceptance_check.py` 会在临时目录里复制样例、注入各类故障（损坏 PNG、删除
PNG、删除 CSV、删除端点列、阈值 -1 等），并打印每项实测数据。退出码 0 表示
全部通过。

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--settings` | 烘焙设定 JSON 路径（必填） |
| `--output` | 输出目录（必填，不存在则创建） |
| `--strict` | 任一贴图或设定未完成时退出码为 1（默认仅打印） |

---

## 2. 输入约定

### 2.1 设定 JSON

```json
{
  "version": 1,
  "threshold": 32,
  "lightmaps": [
    {
      "name": "sample_lightmap_a",
      "image": "sample_lightmap_a.png",
      "seams_csv": "sample_lightmap_a_seams.csv"
    }
  ]
}
```

* `threshold`：**0 到 255 之间的整数**（按亮度单位，见 2.3）。越界、非整数、
  字符串、布尔、缺失都会让整份报告判为未完成，并在 `reason` 中写明
  `阈值无效`。
* `lightmaps` 顺序即处理顺序（要求 1）。
* `image` / `seams_csv` 相对于 JSON 所在目录解析，也接受绝对路径。
  `seams_csv` 可简写为 `seams`。
* `name` 可省略，默认取贴图文件名去扩展名。

### 2.2 接缝清单 CSV

首行为表头，必须能解析出**四个端点 UV 数值列**。常用写法都能识别：

| 语义 | 可接受的列名 |
| --- | --- |
| 端点1 U | `u1`, `u_a`, `ua`, `u_start`, `start_u`, `x1`, `x_a` |
| 端点1 V | `v1`, `v_a`, `va`, `v_start`, `start_v`, `y1`, `y_a` |
| 端点2 U | `u2`, `u_b`, `ub`, `u_end`, `end_u`, `x2`, `x_b` |
| 端点2 V | `v2`, `v_b`, `vb`, `v_end`, `end_v`, `y2`, `y_b` |

* `seam_id`（或 `id` / `name` / `edge_id`）可选，只用于标注结果；
  缺省按行号生成 `seam_001` 之类。
* 分隔符支持 `,` `;` 制表符（按内容自动嗅探），UTF-8（含 BOM）。
* 判为**格式问题**（该贴图未完成）的情况：缺端点列、某行字段数少于表头、
  坐标不是数值、文件非 UTF-8 或为空。
* 重复接缝：两端点 UV 相同（**交换端点顺序也算同一条**）只计一次，其余行记入
  `duplicates` 与 `duplicate_count`。

### 2.3 UV 与采样规则

* UV 原点在左下角（图像第 0 行是 V=1），`u` 向右、`v` 向上。
  `x = u * (W-1)`，`y = (1-v) * (H-1)`。
* 采样位置：沿接缝从 10% 到 90% 共 9 个基准点（不取端点，避免边角像素主导）。
  每点沿接缝**法线**方向各取 2 px 的两个对侧像素。
* 亮度：`0.2126*R + 0.7152*G + 0.0722*B`（Rec.709，灰度贴图即为灰度值）。
* 判定：`max(|side_a - side_b|)` **严格大于** 阈值才算漏光候选；
  `math.isclose` 容差 1e-6 仅用于消除浮点噪声，因此**差值恰好等于阈值视为未漏光**。
* **越界**：任一端点在 UV `[0,1]` 之外，或任一采样点落在贴图像素范围外。
  越界接缝不参与漏光判定。
* **退化**：两端点 UV 相同（或像素坐标完全重合），不参与漏光判定。
* 判定顺序：越界 → 退化 → 采样比较。越界和退化都不会产生 `leak`。

### 2.4 采样几何的取舍（写清楚，便于美术核对）

法线采样是数值方法，对**flat 明暗分区**（本任务样例的形态：竖直方向贯穿过
明暗分界）最可靠。已知边界：

* 沿分界线平行、且恰好压在分界线上或落在 `±2px` 过渡带内的接缝，两侧可能都
  采到同一侧像素而不判漏光，也可能因取整落到另一侧。这是"两侧各取 2px"这一欠定
  问题的固有边界，不是实现缺陷。需要更保守判定时，可调大
  `analysis.SAMPLE_OFFSET`、加密 `analysis.SAMPLE_POSITIONS`。
* 越界判定覆盖两种：端点 UV 超出 `[0,1]`，或**任一采样点**（含对侧的镜像点）
  落在贴图像素范围外。后者对应"采样点落在 UV[0,1] 之外或贴图范围外"的要求。
* 退化判定先于采样：两端点 UV 相同即退化；两端点 UV 不同、但在贴图像素坐标下
  完全重合（`|Δx| = |Δy| = 0`）时也记为退化（`两端点在贴图上重合`）。

---

## 3. 输出

### 3.1 标注图 `<贴图名>_seam_report.png`

原光照贴图的副本（RGB），**原图字节不改**。只使用两种颜色：

| 类别 | 颜色 |
| --- | --- |
| 漏光接缝 | 纯红 `rgb(255,0,0)` |
| 越界接缝 | 纯蓝 `rgb(0,0,255)` |

线宽 3 px，两端各画 4 px 圆点。越界接缝的端点会先钳制到画布边缘再绘制，
因此越界接缝总是可见。无任何问题的贴图仍会生成标注图，尺寸一致、不含调色板
像素；其**解码后像素与原图逐字节相同**（PNG 会被重新编码，所以文件字节不保证
相同）。

### 3.2 `seam_result.json`

```jsonc
{
  "status": "completed | incomplete",
  "settings_valid": true,
  "reason": null,
  "settings": "...bake_settings.json",
  "threshold": 32,
  "output_dir": "...",
  "report_file_pattern": "<lightmap stem>_seam_report.png",
  "annotation_palette": { "light_leak": "rgb(255, 0, 0)", "out_of_bounds": "rgb(0, 0, 255)" },
  "summary": {
    "total_lightmaps": 3,
    "completed_lightmaps": 3,
    "incomplete_lightmaps": 0,
    "total_leaks": 1
  },
  "lightmaps": [
    {
      "name": "sample_lightmap_a",
      "image": "sample_lightmap_a.png",
      "seams_csv": "sample_lightmap_a_seams.csv",
      "status": "completed",
      "reason": null,
      "total_seams": 5,          // 去重后的接缝数
      "row_count": 6,            // 清单数据行数
      "leak_count": 1,
      "out_of_bounds_count": 1,
      "duplicate_count": 1,
      "report_file": "sample_lightmap_a_seam_report.png",
      "seams": [
        { "seam_id": "leak_center", "line": 2, "u1": 0.5, "v1": 0.15,
          "u2": 0.5, "v2": 0.85,
          "classification": "leak",
          "reason": "漏光候选：两侧亮度差 160.00 超过阈值 32",
          "max_luminance_a": 210.0, "max_luminance_b": 50.0,
          "max_difference": 160.0, "mean_difference": 160.0 }
      ],
      "duplicates": [
        { "seam_id": "leak_center_copy", "line": 7, "duplicate_of": "leak_center" }
      ]
    }
  ]
}
```

`classification` 取值：`leak` / `out_of_bounds` / `degenerate` / `ok`。
完成了采样的接缝都会带 `max_difference` 等亮度证据（含未超阈值的），
越界/退化的接缝不带（因为没有采样）。

---

## 4. 设计要点

* **单贴图隔离**：`run_analysis` 对每个贴图独立 try/except，任何异常都只把该
  贴图记为未完成（`处理异常: ...`），后续贴图继续。
* **先解析清单再读图**：贴图损坏时仍能报出该清单的接缝数/重复数。
* **未完成不产标注**：未完成的贴图会在输出目录中删除上次遗留的同名标注图，
  避免"有标注图就代表本次成功"的误读。输入图从不删除。
* **设定级失败**：阈值无效、JSON 无法解析等会让整份报告 `status=incomplete`，
  `summary` 仍按条目统计（全部计入未完成），并在每条 `reason` 写明。
* **确定性**：样例无随机数、无时间戳；同一输入两次运行的 `lightmaps` 载荷逐
  字节相同（路径字段除外）。

## 5. 目录

```
lightmap_seams/       # 包：config / seams / analysis / annotation / report / cli / samples
tools/                # generate_samples.py, run_check.py, acceptance_check.py
tests/test_acceptance.py
samples/              # 已提交的确定性样例输入
out/sample/           # 样例的实际检出结果（标注图 + seam_result.json）
```

## 6. 验收对照

| # | 验收点 | 实现 | 可复现证据 |
| --- | --- | --- | --- |
| 1 | 单张贴图损坏不中断 | `report._analysis_report` 逐贴图 try/except | `test_01_corrupt_*`（PerLightmapFailureTests） |
| 2 | 明暗突变接缝判漏光 | `analysis.classify_seam`，取 9 点两侧最大值 | 样例 A `leak_center`（差值 160.00） |
| 3 | UV/贴图越界记越界且不判漏光 | `_out_of_bounds_reason` + 采样点范围检查 | 样例 A `out_of_bounds`、`SamplePointBoundsTests` |
| 4 | 两类接缝颜色可区分 | `annotation.py` 红/蓝两色 | 样例 A 标注图红 1166 px、蓝 375 px |
| 5 | CSV 缺列 / 行字段不足 | `seams.parse_seams` | `test_05_*`、acceptance_check |
| 6 | PNG 缺失/坏图 → 未完成且无标注 | `report._process_one`，删除遗留标注 | `test_06_*`、`test_01_*` |
| 7 | 阈值越界/非数值 → 整体未完成 | `config._threshold_error` | `SettingsFailureTests`、`threshold -1` |
| 8 | 重复端点对只计一次 | `seams._duplicate_key`（含反向） | 样例 A 行 7 / 去重后 5，重复 1 |
| 9 | 差值等于阈值不算漏光 | `max_difference > threshold` + isclose 1e-6 | 样例 C `c_exact_threshold` 差值 32.0 |
| 10 | 逐贴图状态与计数 | `report._entry` | `seam_result.json` |
| 11 | `_seam_report.png`、输入不改 | `LightmapSpec.report_name` | 前后 SHA-256 断言 |
| 12 | 两端点相同 → 退化 | `_is_degenerate` | 样例 A `degenerate_point` |
| 13 | 缺 CSV → 未完成，其余继续 | `report._process_one` | `test_13_*` |
| 14 | 顶层四项汇总一致 | `report._summarise` | `test_14_*` |
| 15 | 标注图尺寸一致；无问题图无标注色 | `annotate` 在副本上只画问题接缝 | 样例 B/C 与原图像素一致 |

`python tools/acceptance_check.py` 的实测输出，17/17 通过：

```
Baseline: deterministic sample
  PASS  10 result JSON parses and carries counters  [{"total_lightmaps": 3, "completed_lightmaps": 3, "incomplete_lightmaps": 0, "total_leaks": 1}]
  PASS  2  brightness step seam flagged  [seams=5 leaks=1 oob=1 dup=1]
  PASS  3  UV 1.2 is out of bounds, not a leak  [坐标越界：端点 u1=1.2 的 u 超出 UV[0,1]]
  PASS  9  difference == threshold is not a leak  [leak_center max_difference=160.0]
  PASS  12 identical endpoints degenerate  [退化接缝：两端点 UV 相同，不参与漏光判定]
  PASS  8  copied seam counted once  [rows=6 seams=5 dup=1 leaks=1]
  PASS  4  leak red + out-of-bounds blue both present  [red_px=1166 blue_px=375]
  PASS  11 inputs untouched, naming pattern  [sample_lightmap_a_seam_report.png, sample_lightmap_b_seam_report.png, sample_lightmap_c_seam_report.png]
  PASS  14 summary equals detail  [{"total_lightmaps": 3, "completed_lightmaps": 3, "incomplete_lightmaps": 0, "total_leaks": 1}]
  PASS  15 sample_lightmap_b.png clean annotation matches input size  [size=(512, 512) palette_px=0]
  PASS  15 sample_lightmap_c.png clean annotation matches input size  [size=(512, 512) palette_px=0]
  PASS  11 input bytes unchanged after run

Fault injection
  PASS  1  corrupt lightmap does not stop the rest  [光照贴图无法解码：sample_lightmap_a.png（cannot identify image file ...）]
  PASS  5  missing endpoint columns marks incomplete  [接缝清单列缺失：缺少必要列 u2、v2（需要端点 UV 四列 u1、v1、u2、v2；表头位于第 1 行）]
  PASS  6  missing PNG -> incomplete, no annotation  [光照贴图缺失：sample_lightmap_b.png]
  PASS  13 missing seam CSV -> incomplete, others continue  [接缝清单缺失：sample_lightmap_c_seams.csv]
  PASS  7  threshold -1 -> whole report incomplete  [阈值无效：-1 超出范围；阈值必须为 0 到 255 之间的整数]

Result
  17/17 checks passed, 0 failed
```
