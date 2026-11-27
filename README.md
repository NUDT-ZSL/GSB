# 水下教学素材镜头切分与能见度分段

本仓库实现一条完全确定性、离线运行的 Python/NumPy 流水线，用于处理水下训练视频的逐帧整数像素缩略图序列。流水线依次完成：

1. 颜色直方图距离与边缘密度变化计算，并合成镜头边界度量；
2. 自适应阈值切点检测、单帧假切点删除、过短镜头合并；
3. 局部对比度和暗通道先验能见度评分及浑浊等级划分；
4. 镜头评分序列的分段常数拟合与教学段落归并；
5. 镜头切换、浑浊涌入/恢复两类事件的相对证据裁定；
6. 可复核 JSON/CSV/Markdown 报告输出；
7. 同种子逐项复现校验与第二种子结构一致性校验。

样本生成、分析和报告均只使用 Python 标准库与 NumPy，不访问网络或外部素材。

## 环境与运行

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

默认命令会：

- 使用主种子 `20240517` 生成并分析 480 帧、8 fps、`72x96x3` 的水下缩略图；
- 对同一主种子二次生成、二次分析并逐项比较边界、评分、段落和事件；
- 使用第二种子 `20240518` 生成不同浑浊分布并验证同一套参数可正常运行；
- 将结果写入 `outputs/seed_20240517/`、`outputs/seed_20240518/` 和 `outputs/run_summary.json`。

常用变体：

```bash
# 快速运行，不做同种子二次回放
python3 main.py --skip-replay

# 自定义两个种子与帧数
python3 main.py --seed 20240517 --compare-seed 20240518 --n-frames 480

# 分析已有 NPZ（必须含 frames 与 timestamps，可选 truth_json）
python3 main.py --input-npz path/to/sample.npz --out-dir outputs/custom

# 单元验收
python3 -m unittest discover -s tests -v
```

## 输入约定

`--input-npz` 读取以下字段：

- `frames`: 形状 `(N,H,W,1)` 或 `(N,H,W,3)` 的整数数组，取值 `[0,255]`；
- `timestamps`: 一维、有限、严格单调递增的采样时间戳，单位秒；
- `truth_json`: 可选，UTF-8 JSON 字符串，用于额外的真值核对。

生成器保存的 `sample.npz` 可直接作为输入。业务错误不会静默成功，CLI 会返回稳定错误码并写入 `error.json`：

| 错误码 | 退出码 | 含义 |
| --- | ---: | --- |
| `E_EMPTY_FRAMES` | 10 | 帧或时间戳为空 |
| `E_TIMESTAMPS_NOT_MONOTONIC` | 11 | 时间戳非严格递增 |
| `E_TIMESTAMP_LENGTH_MISMATCH` | 12 | 帧数与时间戳数不一致 |
| `E_INVALID_FRAME_SHAPE` | 13 | 帧维度/通道不合法 |
| `E_NON_INTEGER_FRAMES` | 14 | 像素矩阵不是整数类型 |
| `E_INSUFFICIENT_FRAMES` | 15 | 少于两帧 |
| `E_INVALID_TIMESTAMPS` | 16 | 时间戳含 NaN/Inf 或不是数值 |
| `E_INVALID_INPUT` | 17 | NPZ、像素范围或其他输入错误 |

JSON 中 `frame_index` 为 0 起序号，`frame_number` 为 1 起帧号；Markdown 报告的事件与边界表使用 1 起帧号。

## 输出文件

每个种子目录包含：

- `sample.npz`：确定性生成器的帧、时间戳和真实切分/浑浊标记；NPZ 使用固定 ZIP 元数据，可做到同种子文件字节级复现；
- `analysis.json`：完整内核结果；
- `shot_boundaries.json`：原始边界、修正后边界、逐条修正差异和镜头范围；
- `visibility_scores.json`：逐镜头评分、等级、局部对比度/暗通道统计及拟合结果；
- `teaching_segments.json`：教学段落和浑浊突变点；
- `events.json`：去重后的带类型事件表、碰撞证据和浑浊区间；
- `frame_metrics.csv`：逐帧直方图距离、边缘密度、边界度量、阈值和能见度；
- `shot_scores.csv`：逐镜头评分与统计量；
- `events.csv`：事件表；
- `report.json` / `report.md`：复核报告，包含段落、镜头归属、等级跨度和事件。

`outputs/run_summary.json` 记录所有产物路径、真值核对、同种子复现检查和两种子结构比较。

## 方法说明

### 镜头边界

每帧按 RGB 三通道各 16 个 bin 统计归一化颜色直方图；相邻帧的直方图距离取三通道总变差距离的均值。边缘密度由灰度中心差分梯度在固定阈值上的占比计算，相邻帧取绝对差。

联合度量为：

```text
boundary_metric =
    0.72 * histogram_distance
  + 0.28 * min(edge_density_change / 0.14, 1.0)
```

自适应阈值同时使用全局中位数/MAD 与局部窗口中位数/MAD。高于阈值且为局部显著的帧进入原始切点清单；连续 1–3 帧的显著区段全部保留，以便识别单帧曝光闪断产生的成对假切点。

稳定化使用真实采样时间戳：单帧片段会删除其前后两个切点；过短片段按帧数和秒数双重阈值并入证据较弱的相邻片段。全部“原始 -> 稳定”的保留/删除项逐条写入 `correction_changes`。

### 能见度

每帧计算 8×8 分块的灰度局部对比度均值，以及 RGB 最小通道经半径 2 最小值滤波后的暗通道统计。评分区间为：

```text
score = 100 * (0.55 * normalised_contrast + 0.45 * dark_channel_score)
```

其中暗通道越亮（雾感越强），`dark_channel_score` 越低。镜头评分为逐帧评分的均值，并输出标准差、分位数、局部对比度、暗通道均值/90 分位、饱和度等关键统计量。

等级阈值：

| 等级 | 分数区间 |
| --- | --- |
| `clear` | `>= 65` |
| `light_turbidity` | `[48, 65)` |
| `moderate_turbidity` | `[30, 48)` |
| `heavy_turbidity` | `< 30` |

### 分段与事件

镜头评分序列使用带固定段惩罚和“相邻镜头分数变化不超过 10 分”约束的精确动态规划，拟合分段常数。每个教学段落只包含连续镜头，并记录等级跨度、事件和浑浊突变来源。

逐帧平滑能见度序列独立检测持续低能见度区间，用区间起止定位浑浊涌入/恢复。若原始镜头边界与浑浊渐变邻域同时命中，则比较归一化边界度量与评分跳变证据：

- `cut_evidence = boundary_metric`；
- `visibility_evidence = abs(smoothed_score_jump) / 50`；
- 评分证据超过边界证据的 `1.05` 倍时，原始切点被归类为能见度变化并从稳定镜头边界中剔除。

最终事件表在同一帧只保留一个事件类型。浑浊区间清单仍单独保留，从而不会因为药雾/浑水涌入而错误截断教学章节。

## 固定种子验收基线

### 主种子 `20240517`

- 真实切点：`[90, 178, 272, 378]`
- 修正后切点：`[90, 178, 272, 378]`，精确匹配；
- 原始切点：`[90, 178, 199, 200, 266, 272, 351, 356, 359, 378]`；
- 单帧曝光闪断：`199/200` 两个边界被成对删除；
- 浑浊渐变伪峰：`266, 351, 356, 359` 被裁定为能见度事件；
- 真实浑浊区间：`[258, 360)`；检测区间：`[257, 361)`；
- 镜头评分等级：`S01 clear, S02 clear, S03 light_turbidity, S04 moderate_turbidity, S05 clear`；
- 教学段落：`P01=S01-S03`、`P02=S04`、`P03=S05`。

### 第二种子 `20240518`

- 真实切点：`[101, 206, 294, 384]`，修正结果精确匹配；
- 包含两段不同浑浊分布，检测为 `[172, 258)` 与 `[348, 438)`；
- 结构、字段和一致性检查与主种子相同，但边界、评分、段落、事件及内容哈希均随样本变化。

运行 `python3 main.py` 后，`outputs/run_summary.json` 中的 `reproducibility[*].checks` 必须全部为 `true`，`seed_comparison.same_structure` 与 `both_consistency_passed` 也必须为 `true`。

## 测试覆盖

`tests/test_pipeline.py` 覆盖：

- 主种子切点精确恢复与真值比对；
- 单帧假切点删除、浑浊伪峰过滤；
- 同种子逐项复现；
- 第二种子不同浑浊分布；
- 镜头归属三方一致；
- JSON/CSV/Markdown 产物生成；
- 空帧与非单调时间戳的稳定业务错误码。
