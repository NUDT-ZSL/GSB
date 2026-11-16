# 本地多波段遥感瓦片增量语义分割管线

该工作区实现了一个无网络、无容器、确定性可复现的多波段 PNG 瓦片语义分割管线。管线支持滑窗特征提取、像素级分类、跨瓦片重叠融合、全局类别后处理、内容寻址缓存、局部重算预算、版本化快照、可处理退出信号与强制终止恢复。

固定种子样本已生成在 `sample/`，批量编辑序列已验证在每次编辑后从现行增量状态收敛，并与同一最终输入的全量运行逐像素一致。

## 目录

```text
rsseg/                      核心实现
  config.py                 配置校验、分类配置指纹
  index.py                  瓦片索引/邻接/PNG 头部校验
  overlay.py                局部波段补丁校验
  tiles.py                  解码、波段重排、内容指纹与缓存键
  features.py               滑窗特征、确定性分类
  fusion.py                 距离加权投票、连通域后处理
  state.py                  瓦片/融合缓存、manifest、原子快照
  engine.py                 失效判定、预算调度、线程池、诊断
  sequence.py               编辑序列应用与增量/全量比较
  sample.py                 固定种子样本与编辑序列生成
scripts/                    生成、编辑序列、验收脚本
tests/test_pipeline.py      15 项自动化测试
sample/                     3x3 固定种子样本，64x64、四波段、16 像素重叠
artifacts/                  实际运行生成的验收与诊断报告
```

## 环境与离线安装

代码只依赖 Python 3.11、NumPy 和 Pillow。本次实际执行环境已经建立项目内 `.venv`：

```text
Python 3.11.0rc1
NumPy 2.4.6
Pillow 13.0.0
```

依赖来自本机已有的离线安装目录，启动方式：

```bash
.venv/bin/python -m rsseg --help
```

Windows 上可将与 CPython 3.11、目标架构匹配的 NumPy/Pillow wheel 放入本地 `wheels/`，然后执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_offline.ps1 -WheelHouse wheels
```

Linux/WSL 等价脚本为 `scripts/bootstrap_offline.sh`。两者均使用 `pip --no-index --find-links`，不访问网络。

说明：本次实际测试运行在 WSL/Ubuntu 的 CPython 3.11 环境。实现本身使用 `pathlib`、`os.replace`、线程池和跨平台 Pillow/NumPy API，未使用 POSIX 专用路径格式；Windows 复验需提供 Windows 对应的 cp311 wheel。

## 快速开始

```bash
# 生成固定种子样本（会恢复初始 config/index/overlay）
.venv/bin/python scripts/generate_sample.py --out sample

# 从空白状态全量运行
.venv/bin/python -m rsseg full --workspace sample --state states/full

# 每次只执行一个预算批次
.venv/bin/python -m rsseg run --workspace sample --state states/inc --max-batches 1

# 持续执行，直到没有待重算项
.venv/bin/python -m rsseg run --workspace sample --state states/inc --max-batches 0

# 比较增量快照和全量快照的全局类别与全部瓦片得分
.venv/bin/python -m rsseg verify --left states/full --right states/inc
```

`run` 在预算不足时写出可用快照，报告 `E_RECOMPUTE_BUDGET_PENDING`；再次执行同一命令会复用已有结果并继续。

## 输入模式

### 瓦片索引 `tiles.json`

```json
{
  "index_version": "sample-20261117-v1",
  "band_version": "seed-20261117-bands-v1",
  "band_order": ["red", "green", "nir", "elevation"],
  "tile_size": [64, 64],
  "tiles": [
    {
      "row": 0,
      "col": 0,
      "path": "tiles/r0_c0.png",
      "bounds": [0, 0, 64, 64],
      "band_version": "seed-20261117-bands-v1",
      "band_order": ["red", "green", "nir", "elevation"],
      "neighbors": [[0, 1], [1, 0], [1, 1]]
    }
  ]
}
```

`bounds` 为 `[xmin, ymin, xmax, ymax]`。加载层校验：

- 行列号非负且唯一；
- bounds 尺寸与 `tile_size` 一致；
- 邻接目标存在、无自邻接、无重复，且邻接关系双向对称；
- PNG 为 RGBA 四波段、尺寸与 bounds 一致；
- `band_order` 是 `red/green/nir/elevation` 的完整排列。

索引结构、重复行列号或非对称邻接产生 `E_INDEX_INVALID`。PNG 模式、波段数或尺寸错误产生 `E_TILE_SHAPE_MISMATCH`。失败瓦片进入稳定排序的失败清单，其他瓦片继续推理。

### 分割配置 `config.json`

关键字段如下：

```json
{
  "window_size": [15, 15],
  "stride": [8, 8],
  "context_overlap": 1,
  "feature_thresholds": {
    "ndvi": 0.30,
    "ndwi": 0.12,
    "edge_density": 0.055,
    "slope": 0.035,
    "brightness": 0.42,
    "elevation": 0.30
  },
  "class_weights": {"1": {"bias": 0.36, "ndvi": 4.6}},
  "fusion_strategy": "distance_weighted_vote",
  "min_connected_area": 10,
  "concurrency": 3,
  "recompute_budget": 2
}
```

分类配置指纹只覆盖会影响逐瓦片特征/得分的字段：窗口、步长、上下文、阈值、权重、epsilon 与特征缩放。融合策略、最小连通面积、并发数和预算变化不会触发瓦片特征重算。

### 叠加补丁 `overlay.json`

补丁可按索引波段顺序提供 band-major `bands`，或按像素提供 `values`：

```json
{
  "patches": [
    {
      "id": "p1",
      "tile": [1, 1],
      "bbox": [8, 10, 31, 29],
      "bands": [[[12, 13], [14, 15]], [[0, 0], [0, 0]], [[0, 0], [0, 0]], [[0, 0], [0, 0]]]
    }
  ]
}
```

越界坐标、补丁目标不存在、波段数量不等于 4、矩形尺寸与数据形状不符时记录 `E_PATCH_OUT_OF_RANGE`。该补丁被忽略；若没有其他有效补丁，该瓦片的旧缓存键、类别图和得分图保持不变。

## 确定性算法

### 滑窗与特征

每个瓦片按 `stride` 产生稳定顺序的窗口，窗口覆盖 `window_size`，并向外读取 `context_overlap` 像素作为边缘/坡度上下文。边缘使用 reflect padding（退化轴使用 edge）。

逐像素特征：

- `NDVI = (NIR - Red) / (NIR + Red + epsilon)`
- `NDWI = (Green - NIR) / (Green + NIR + epsilon)`
- 边缘密度：NIR 中心差分梯度幅值阈值化后做 3x3 局部均值
- 坡度：高程中心差分梯度幅值，缩放到 `[0, 1]`
- 辅助特征：亮度和高程

每个类别按配置权重计算：

```text
score[c] = bias[c] + sum(weight[c,f] * (feature[f] - threshold[f]))
```

得分统一四舍五入到 10 位小数，避免不同平台 FMA 实现影响平票。`argmax` 在类别编号 `1..4` 上选择，因此同分固定选择编号最小的类别。全过程不使用随机数。

### 跨瓦片融合

每个瓦片 bound 形成一个可缓存的融合区域。区域参与者是所有与该 bound 相交的瓦片，按 `(row, col)` 字典序处理。

`distance_weighted_vote` 的投票权重：

```text
weight = min(x + 1, y + 1, width - x, height - y)
```

其中 `x/y` 是全局像素在该瓦片内部的坐标。也支持 `uniform_vote`。某类别票数唯一最高时直接胜出；最高票平票时，选择最早（行列号字典序最小）为其投票的瓦片所属类别。

### 类别后处理

对融合类别栅格按四邻接搜索连通域。面积小于 `min_connected_area` 的非背景连通域被替换为边界邻接像素中出现次数最多的类别；次数相同选类别编号较小者。所有替换基于同一原始栅格计算，因此结果与线程调度无关。

## 增量状态与缓存

### 瓦片缓存键

```text
tile_cache_key = sha256(
    band_content_fingerprint + index band_version + band_order +
    classification_fingerprint + tile shape
)
```

`band_content_fingerprint` 由 PNG 原始文件内容哈希和该瓦片有效补丁内容哈希组成。因此文件内容、有效补丁、波段版本、波段顺序或分类配置变化时仅相关瓦片失效。

### 融合依赖指纹

```text
fusion_dep = sha256(region bounds + strategy +
                    participant tile cache keys +
                    participant adjacency digests)
```

因此瓦片结果、空间重叠参与集合、融合策略或相关邻接关系变化会使对应融合区域失效。仅修改最小连通面积时不重算瓦片或预融合区域，只重做全局后处理。

### 预算单位

局部重算预算以“单位”计数：一次瓦片重算或一次已具备全部最新参与瓦片的融合区域重算各占 1。候选单位按 `(row, col, tile-before-fusion)` 稳定排序，每批次取前 `recompute_budget` 个。

瓦片结果尚未全部更新时，依赖这些瓦片的融合区域不会被提前重算，避免写入必将在下一批失效的融合结果。未完成项写入 snapshot manifest 和诊断报告。

未失效瓦片只从 manifest 指向的完整缓存加载；报告中的 `feature_extractions` 是实际执行特征提取的次数，复用瓦片不计入。

## 快照、恢复与退出

状态目录：

```text
state/
  tile_results/r{row}_c{col}/{cache_key}.npz
  fusion_results/r{row}_c{col}/{dep_fingerprint}.npz
  snapshot/
    manifest.json
    category.npy
    category.png
    tile_results.npz
    diagnostics.json
```

每个瓦片/融合缓存先写同目录临时文件，再 `os.replace` 原子替换。全局快照先完整写入 `.snapshot-staging-*`，随后把旧 snapshot 改名为备份、将 staging 改名为 snapshot、最后删除备份。启动时若 snapshot 缺失而备份存在则恢复备份。

manifest 记录：

- 全局 `snapshot_version`；
- 配置哈希、分类配置指纹、索引哈希/版本、波段版本、融合策略；
- 每个瓦片的缓存键、期望缓存键、波段内容指纹、结果版本和 current/stale 状态；
- 每个融合区域的依赖指纹、期望依赖指纹和结果版本；
- 未收敛瓦片/融合区域；
- 快照内文件的 SHA-256 校验和。

读取快照时校验 manifest 及文件校验和。强制终止后不信任未出现在合格 manifest 中的孤儿缓存，只复用 manifest 指向且完整落盘的瓦片结果。

CLI 捕获 `SIGINT`/`SIGTERM`：已提交的当前瓦片任务完成并落盘，随后写出 snapshot/manifest，以整数退出码 `0` 返回，状态标记为 `interrupted`。重启后会继续未收敛项。

## 业务码与状态

- `E_INDEX_INVALID`：索引、重复行列号、邻接不对称等。
- `E_TILE_SHAPE_MISMATCH`：PNG 波段/尺寸/解码或单瓦片处理异常。
- `E_PATCH_OUT_OF_RANGE`：补丁越界、长度/形状不合法。
- `E_RECOMPUTE_BUDGET_PENDING`：仍有未收敛项。
- `E_STATE_INVALID`：恢复时忽略校验失败的旧快照。

已处理运行统一返回进程码 `0`，由诊断报告的 `status`、`converged` 和业务码表达结果；配置不可读等未处理输入错误由 CLI 返回非零。

## 诊断报告

完整报告写到 `{state}/diagnostics.json`，并存入对应 snapshot。主要字段：

- `status`、`converged`、`business_codes`、`snapshot_version`；
- `batches[]`：每批失效/复用/重算瓦片列表和计数、失效/重算融合区域、未收敛项、特征提取次数、批次阶段耗时；
- `timings_ms`：索引校验、瓦片校验、指纹、特征/分类、融合、后处理、快照及总耗时；
- `area_stats`：四类面积；
- `unconverged`：待处理瓦片和融合区域；
- 稳定排序的 `failures[]`；
- 快照路径和 manifest SHA-256。

## 实际验证结果

### 自动化测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：`Ran 15 tests ... OK`。覆盖固定种子字节确定性、线程并发一致性、低预算多批收敛、快照校验/备份恢复、中断快照、非法补丁保持旧指纹、合法补丁增量/全量一致、索引非对称/重复行列号、单瓦片错误隔离、融合平票和连通域后处理。

### 预算验收

报告：`artifacts/acceptance_report.json`。初始样本、`recompute_budget=2`：

```text
incremental_calls = 9
pending_observed_before_convergence = true
snapshot_versions = 1..9
category_equal = true
tile_set_equal = true
tile_classes_equal = true
tile_scores_equal = true
area_stats = {1: 18151, 2: 4349, 3: 2067, 4: 1033}
```

各次调用重算瓦片数为 `[2, 2, 2, 0, 1, 2, 0, 0, 0]`；为 0 的批次在重算已就绪的融合区域，预算单位语义为“瓦片 + 融合区域”。

### 六步编辑序列

报告：`artifacts/sequence_report.json`。依次编辑阈值、道路权重、索引波段版本与邻接、合法局部补丁、非法补丁、最小连通面积与恢复补丁。每一步都运行到收敛，并从独立空状态运行同一最终输入的全量基线。

```text
step 1 threshold-tune                 calls=9 equal=true pending=true snapshot=9
step 2 road-weight-tune               calls=9 equal=true pending=true snapshot=18
step 3 index-version-and-adjacency    calls=9 equal=true pending=true snapshot=27
step 4 valid-local-band-patch         calls=6 equal=true pending=true snapshot=33
step 5 invalid-patch-isolated         calls=6 equal=true pending=true snapshot=39
step 6 recover-and-postprocess        calls=9 equal=true pending=true snapshot=48
all_equal = true
```

每步比较了全局类别栅格、全部瓦片类别图、全部瓦片得分图和瓦片缓存键；均逐像素/逐元素一致。非法补丁步骤输出 `E_PATCH_OUT_OF_RANGE`，目标瓦片未重算且旧键保持不变。

### 信号与重启

- 运行中发送 `SIGTERM`：进程退出码 `0`，状态 `interrupted`，snapshot 版本 1 可校验；当时已完成 2 个瓦片。
- 同一状态再次运行后收敛，最终与全量快照 `category_equal=true`、`tile_scores_equal=true`，最终版本 9。
- `SIGKILL` 中断后无有效 manifest，重启仅从空状态重建，最终仍与全量逐像素一致。

相关报告：`artifacts/signal_resume_summary.json`、`artifacts/force_resume_summary.json`、`artifacts/cli_verify.json`。

## 已知边界

- 样本为本地像素坐标，不包含真实地理投影/重采样。
- PNG 使用 RGBA 通道承载红、绿、近红外、高程四个 8 位波段；高程为量化值。
- 连通域后处理目前面向本地固定尺寸样本，采用四邻接全图扫描，未使用 GIS/形态学扩展依赖。
- 代码面向小到中等本地瓦片集合；全局类别输出内存占用与总空间范围成正比。
