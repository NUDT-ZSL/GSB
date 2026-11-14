# 封面候选选取工具（cover-selector）

本地命令行工具：读取导出的逐帧 PNG 序列与候选时间点清单，按配置的帧率定位帧、
合并近似重复画面、按画面差异强度排序导出封面候选图，并生成逐条核对报告。

- 纯本地运行，不依赖网络或容器。
- 只读源帧与清单，所有产物写入指定输出目录。
- 报告同时输出 Markdown（人读）、JSON（机读）、HTML（浏览器查看）。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上 |
| 依赖 | Pillow、numpy |
| 平台 | Windows / macOS / Linux 均可（本地已验证 Windows 11 + Python 3.10.20） |

安装依赖：

```bash
python -m pip install "Pillow>=9.1" "numpy>=1.21"
```

## 快速开始

```bash
# 1. 生成样例（只需一次；会写入 samples/）
python generate_samples.py

# 2. 运行工具
python -m cover_selector \
  --frames samples/frames \
  --manifest samples/manifest.json \
  --config samples/config.json \
  --output out/run1
```

Windows PowerShell 中同样可以直接使用上述命令（`\` 换行符请去掉或改用单行）。

运行结束后 `out/run1/` 中会得到：

```
cover_01_hero_2s.png            按差异强度排名第 1 的封面候选
cover_02_product_0.4s.png       ……
cover_03_opening_0.2s.png
cover_04_static_a_0.8s.png
covers_report.md                核对报告（Markdown）
covers_report.html              核对报告（HTML，浏览器直接打开）
covers_report.json              核对报告（结构化数据）
```

本次运行的报告实例见 `out/run1/`、`out/edge/`（边界清单）与 `out/edge_strict/`（严格阈值对比）；
这些目录属于演示产物，已在 `.gitignore` 中忽略。

## 命令行参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--frames` | 是 | 帧序列目录（PNG/JPG/BMP/TIFF/WEBP） |
| `--manifest` | 是 | 候选时间点清单（JSON） |
| `--config` | 是 | 配置文件（JSON） |
| `--output` | 是 | 输出目录，不存在时自动创建 |
| `--threshold` | 否 | 覆盖配置中的 `dedupe_threshold`，用于对比不同阈值 |
| `--resize-mode` | 否 | 覆盖配置中的 `resize_mode`（`fit` / `stretch`） |
| `--iteration` | 否 | 本次运行名称，追加到报告文件名（如 `covers_report__edge.md`），便于保留多组结果 |

退出码：`0` 正常完成（个别清单条目失败不影响退出码），`2` 配置/清单/目录等前提错误。

## 配置文件

```json
{
  "fps": 25,
  "base_width": 640,
  "base_height": 360,
  "dedupe_threshold": 0.02,
  "tolerance_frames": 0.5,
  "frame_start": 0,
  "resize_mode": "fit"
}
```

| 字段 | 必填 | 含义 |
| --- | --- | --- |
| `fps` | 是 | 帧率。目标帧号 = `frame_start + 时间点 × fps`，四舍五入取整 |
| `base_width` / `base_height` | 是 | 基准尺寸；尺寸不一致的帧统一到该尺寸后参与评分与导出 |
| `dedupe_threshold` | 是 | 近似重复阈值，0–1，表示两帧平均像素差（灰度 0–255 归一化）；越小越严格 |
| `tolerance_frames` | 否 | 帧号容差，默认 0.5。仅在目标帧号缺失时，允许命中差值不超过该值的邻近帧，并在报告中注明 |
| `tolerance_seconds` | 否 | 同上，以秒为单位（加载时按 fps 换算为帧）；与 `tolerance_frames` 只能配置其一 |
| `frame_start` | 否 | 首帧编号，默认 0 |
| `resize_mode` | 否 | `fit`（默认，等比缩放 + 画面平均色留边）或 `stretch`（拉伸） |

## 清单格式

```json
{
  "items": [
    {"label": "opening", "time": 0.2, "note": "开场"},
    {"label": "product", "time": 0.4}
  ]
}
```

- 顶层可以是 `{"items": [...]}`、`{"candidates": [...]}`、`{"times": [...]}` 或直接数组。
- 条目可以是对象（`time` 必填，`label`/`note` 任意），也可以直接写数值时间。
- 单条缺字段、时间非数值或越界，只影响该条：其余条目照常处理，报告给出原因。
- 文件名中最长的数字段即帧号（如 `frame_0050.png` → 帧 50，`shot_v2_frame_0012.png` → 帧 12）；不含数字的文件接在最大帧号之后按文件名字典序编号。

## 处理规则

1. **定位**：每个时间点换算为目标帧号，优先精确命中；仅在容差内允许邻近帧替代。
2. **越界/缺失**：超出序列范围或容差内无文件时，该条标记为未产出并写明原因。
3. **尺寸统一**：与基准尺寸不同的帧按 `resize_mode` 统一，不中断整批任务，报告中单独列出。
4. **评分**：`评分 = 0.75 × 梯度清晰度 + 0.25 × 灰度对比度`。梯度越强、灰度分布越分散，画面差异越明显，排名越靠前。
5. **近似重复**：按评分从高到低依次考察，与已保留代表帧的平均像素差 ≤ 阈值即并入该组；采用代表帧比较，避免链式误合并。组内保留评分最高者（同分取时间点更早、清单序号更靠前）。
6. **输出命名**：`cover_<排名>_<标签>_<时间点>s.png`，排名即报告中的排序。

## 样例数据

`samples/` 由 `generate_samples.py` 生成，包含：

- `frames/`：60 个 PNG（帧号 0–60，**缺第 25 帧**），其中 59 张为 640×360；第 20、40 帧为几乎相同的静止画面；第 50 帧为 320×180，用于演示尺寸统一。
- `manifest.json`：5 个常规候选点（0.2s / 0.4s / 0.8s / 1.6s / 2.0s）。
- `manifest_edge.json`：7 条边界清单，含越界（99s）、缺失帧（1.0s → 帧 25）、缺 `time` 字段、时间非数值（`"abc"`）。
- `config.json` / `config_strict.json`：同一组参数，阈值分别为 0.02 与 0.002。

常用命令：

```bash
# 边界清单
python -m cover_selector --frames samples/frames --manifest samples/manifest_edge.json \
  --config samples/config.json --output out/edge --iteration edge

# 严格阈值对比（合并数量会减少）
python -m cover_selector --frames samples/frames --manifest samples/manifest_edge.json \
  --config samples/config_strict.json --output out/edge_strict --iteration edge_strict
```

## 报告内容

报告按顺序包含：

1. 运行信息与配置；
2. **清单条目处理结果**：每个时间点的状态（已输出 / 已合并 / 未产出）、关联输出文件、命中帧号、原因或备注；
3. **输出候选与差异强度排序**：排名、评分及分项（清晰度、对比度、平均梯度）、被合并的时间点及其与代表帧的像素差；
4. **尺寸统一记录**：被统一处理的帧及处理方式；
5. **输入文件校验**：清单与帧目录的 sha256，运行前后一致即说明未改动源文件。

## 自动检查

```bash
python -m pytest tests -q   # 14 项单元/集成测试
python verify.py            # 按 10 条验收要点逐项检查，结果写入 docs/verification.md
```

`verify.py` 在临时目录中执行真实命令行并核对报告，运行前后对 `samples/` 做哈希比对。
最近一次实际结果见 [docs/verification.md](docs/verification.md)。

## 目录结构

```
cover_selector/     实现（配置、清单、帧定位、评分、流程、报告、CLI）
samples/            生成的样例帧、清单与配置
tests/              自动测试
verify.py           验收要点核对脚本
generate_samples.py 样例生成脚本
requirements.txt    依赖清单
docs/verification.md 实际检查结果
```

## 已知边界

- 帧号取自文件名中的数字，序列文件名需保持导出时的编号；同一帧号存在多个文件时取文件名排序最前者并在报告中注明。
- 重复判定基于灰度平均绝对差；对同一画面的轻微压缩噪声不敏感，但构图相近而色调差异很大的两帧不会被判为重复。
- 输出目录中已有的同名报告会被覆盖；候选图按排名命名，多次运行共用同一输出目录时会提示未产出的旧图（不会删除）。

