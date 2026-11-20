# Deterministic Pose Pipeline

面向人体关键点与姿态估计的本地、确定性、可诊断多阶段管线。项目不依赖外部数据集或网络资源，样本和热图均由固定种子在本地生成。

管线覆盖：

1. 热图峰值解码与亚像素精修
2. 关键点分组、骨架连通性检查与实例拼接
3. 精确/近似路径自适应取舍与跨路径一致性裁决
4. OKS/PCK 姿态匹配、候选裁决与指标统计
5. 输入/资源校验、跨模块自检、两次运行哈希复现

## 环境

代码要求 Python 3.10+ 和 NumPy。当前实测环境：

```text
Python 3.10.12
pip 26.3
NumPy 1.26.4
平台 Linux (WSL)
```

实现只使用 Python 标准库和 NumPy，不依赖 Docker、数据库或外部服务，可在 Windows 的 Python 3.10+ 解释器中直接运行。本次执行环境不是原生 Windows，因此未声称完成 Windows 原生进程实测；Windows 命令如下：

```powershell
py -3.10 --version
py -3.10 -m pip --version
py -3.10 -m pip install numpy
py -3.10 run_experiment.py --out reports/experiment_report.json
py -3.10 -m pip install pytest
py -3.10 -m pytest
```

Linux/WSL：

```bash
python --version
python -m pip --version
python -m pip install numpy
python run_experiment.py --out reports/experiment_report.json
python -m pip install pytest
python -m pytest
```

## 快速运行

一条命令会完成：

- 按 `configs/baseline.json` 和 `configs/scale_trace.json` 重建确定性样本；
- 写出 `data/baseline/`、`data/scale_trace/` 下的 JSONL 清单和热图数组；
- 跑完整基线、规模变化追踪、精确/近似交叉检查；
- 每套实验连续跑两次并比较完整报告哈希；
- 写出 `reports/experiment_report.json`。

```bash
python run_experiment.py
```

默认输出：

```text
data/baseline/manifest.jsonl
data/baseline/heatmaps/*.npy
data/scale_trace/manifest.jsonl
data/scale_trace/heatmaps/*.npy
reports/experiment_report.json
```

命令行参数：

```text
--baseline-config   基线配置，默认 configs/baseline.json
--scale-config      规模追踪配置，默认 configs/scale_trace.json
--data-root         样本输出根目录，默认 data
--out               总报告路径，默认 reports/experiment_report.json
```

## 输入样本

`manifest.jsonl` 每行是一张图。必需字段包括：

```json
{
  "image_id": "baseline-0000",
  "canvas_shape": [96, 96],
  "num_keypoints": 17,
  "keypoint_meta": [{"index": 0, "name": "nose", "oks_sigma": 0.04}],
  "heatmap_seed": 20261138,
  "occlusion_ratio": 0.05,
  "skeletons": [],
  "candidates": [],
  "num_channels": 34
}
```

- `skeletons`：真值骨架，每具包含 `skeleton_id`、`source`、K 个关键点及显式骨架边。
- 关键点包含 `index`、`x`、`y`、`visible`、`confidence`。
- `candidates`：独立姿态候选，每个包含 `candidate_id`、`source`、`score`，以及 K 个带 `x`、`y`、`score` 的关键点。
- `heatmap_file` / `heatmap_sha256`：由 `heatmap_seed` 重建后核验的目标热图文件及数组哈希。

## 业务码

| 业务码 | 触发条件 |
|---|---|
| `E_DUP_IMAGE` | 清单中 `image_id` 重复 |
| `E_META_LEN` | `keypoint_meta`、骨架关键点或候选关键点数量与 `num_keypoints` 不符 |
| `E_CANVAS_INVALID` | `canvas_shape` 非 `[H, W]`、非正整数、过小或与配置不一致 |
| `E_CONFIG_INVALID` | 配置字段非法，包括 `occlusion_ratio` 越界 |
| `E_COORD_RANGE` | 真值或候选关键点坐标越出画布 |
| `E_THRESHOLD_INVALID` | OKS/PCK 匹配阈值不在 `(0, 1]` |
| `E_RESOURCE_LIMIT` | 图像数、每图候选数、峰值比较或硬内存上限无法安全处理 |
| `E_KEYPOINT_CONFLICT` | 同一峰值/关键点被两个姿态实例占用 |
| `E_SKELETON_DISCONNECT` | 实例缺少关键点、骨架不连通或边距超限 |
| `E_PATH_DRIFT` | 精确与近似路径坐标差超过 `path_tolerance_px` |
| `E_INCONSISTENT` | 跨模块覆盖、匹配引用、指标复算或参考值声明不一致 |

降级不是错误码：报告中的 `DEGRADED_PATH` 为 warning，并附 `degradation_categories` 与 `reasons`。精确参考因规模限制不可用时记录 `REFERENCE_UNAVAILABLE`，不会静默省略。

## 报告概览

每套实验报告包含：

- `decision`：规模观测、精确/近似代价估计、内存估算、资源上限、优先级、所选路径、降级类别和原因；
- `adjudication_outcome`：所选路径 PCK、精确参考 PCK、近似 PCK、观测精度增益、峰值比较节省和内存节省；
- `selected_path_result.decoded`：峰值像素坐标、强度、亚像素偏移、插值口径、比较次数；
- `selected_path_result.grouped`：实例关键点集合、骨架边和可复算边证据；
- `selected_path_result.matching`：匹配指派、未匹配项、阈值和同分打破顺序；
- `selected_path_result.candidate_adjudication`：候选集匹配与每个真值实例的最佳候选；
- `selected_path_result.metrics`：逐图逐点明细、PCK、OKS、逐关键点指标、可见性排除数量；
- `cross_path`：逐关键点坐标偏差、实例 bbox IoU、PCK 差谱或不可用原因；
- `self_check`：覆盖、匹配引用、分母、指标复算和参考声明检查。

总报告还包含 `reproducibility` 和 `content_sha256`。同一固定种子、同一配置下重复执行应得到逐字节一致的 JSON 报告。

## 实测结果

当前 `reports/experiment_report.json` 的结果：

| 实验 | 图像 | 路径 | PCK | 平均 OKS | 降级 | 精确参考 |
|---|---:|---|---:|---:|---:|---:|
| 基线 | 4 | 4 exact | 1.000000 | 0.998517 | 0 | 4/4 |
| 规模追踪 | 8 | 8 approx | 0.737013 | 0.963596 | 8 | 3 有 / 5 不可用 |

规模追踪中近似路径峰值比较总数 `5,762,048`，最大跨路径坐标差 `2.049112 px`，未超过配置容差 `4.0 px`；5 张因候选数超出参考预算而明确记录 `REFERENCE_UNAVAILABLE`。

重复运行：

```text
reports/experiment_report.json
reports/experiment_report_rerun.json
SHA-256: d512b41dd0556d046a87ee93248702577c604a6d5c96c5e5be7df9cc0911a255
cmp: identical
```

测试：

```text
21 passed
```

详细算法和字段契约见 `docs/ALGORITHM.md`。
