# 表格/传感器数据无监督异常检测引擎

本项目从空工作区实现了一个仅依赖 Python 3.10+ 与 NumPy 的本地异常检测实验引擎。它覆盖三种无监督方法、确定性并行调度、比较/内存双重预算、并行串行等价校验、质量评估、结构化错误码，以及三类版本化快照。

所有样本均由固定随机种子在本地生成，不下载外部数据，不使用 Docker 或外部服务。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 1. 生成本地确定性 JSONL 样本
python3 -m anomaly_engine.cli generate --output data/demo.jsonl

# 2. 校验样本与两份配置
python3 -m anomaly_engine.cli validate \
  --dataset data/demo.jsonl \
  --baseline-config configs/demo.json \
  --conflict-config configs/demo_conflict.json

# 3. 跑完整基线与预算冲突降级，并做同种子双次全输出哈希校验
python3 -m anomaly_engine.cli run \
  --dataset data/demo.jsonl \
  --baseline-config configs/demo.json \
  --conflict-config configs/demo_conflict.json \
  --output artifacts/experiment.json \
  --check-rerun

# 4. 运行测试
python3 -m unittest discover -s tests -v

# 5. 独立复现两次并比较文件字节
python3 scripts/reproduce.py
```

Windows 下使用相同的 `python -m ...` 命令即可。进程池显式采用 `spawn`，worker 初始化函数与任务函数均为模块顶层可序列化对象。

## 交付内容

- `anomaly_engine/`：算法、预算裁决、调度、评估、快照与 CLI。
- `configs/demo.json`：完整基线配置。
- `configs/demo_conflict.json`：会触发内存降级和比较预算裁剪的冲突配置。
- `scripts/generate_dataset.py`：本地正态簇与注入异常生成器。
- `scripts/reproduce.py`：生成样本并执行两次，比较完整输出哈希。
- `tests/`：20 项单元/集成测试。
- `docs/DESIGN.md`：算法、预算、调度、快照设计细节。
- `docs/VALIDATION.md`：本次实际验证记录。

## 三种方法

1. `isolation_forest`：自定义孤立森林类随机切分。每棵树的子采样、切分特征、阈值与递归顺序由方法种子驱动的 `numpy.PCG64` 决定。异常分数为 `2^(-平均路径长度 / c(sample_size))`。
2. `knn`：精确欧氏距离，自排除后取最近 `k` 个距离均值，分数越高越异常。按查询行分块，但每行计算不依赖分块边界。
3. `mahalanobis`：全协方差正则化逆矩阵，输出马氏距离。协方差、逆矩阵与工作区内存都进入预算估算；内存不足时不使用近似替代，直接拒绝。

同输入、同参数、同种子下分数统一量化到 12 位小数。排名按“分数降序、`sample_id` 字典序升序”确定。每个方法输出完整 `score_details`、`ranking`、排名明细、有效超参数、种子、分数摘要和 `score_checksum`。

## 预算裁决

比较次数估算（解析式，版本 `analytic-v1`）：

- 孤立森林：`n_trees * sample_size * depth + n_rows * depth`
- KNN：`n_rows * (n_rows - 1) + n_rows * n_neighbors`
- 马氏距离：`n_rows * n_rows * n_features + n_rows * n_features + n_features^3`

内存估算包含输入矩阵、方法中间数组、分数数组和固定工作区，逐项记录在 `resource_estimate.components` 与 `effective_estimate.components` 中。

默认显式优先级为：

1. `quality`：优先配置质量代理更高的候选。
2. `memory`：内存更小者优先。
3. `progress`：比较次数更少者优先。

每种方法先选一个内存安全的最高质量锚点，再处理其余候选。内存超限时，孤立森林降低 `n_trees`，KNN 降低单批行数；比较预算不足时，孤立森林降低 `n_trees`，KNN 降低 `n_neighbors`；马氏距离无近似降级，直接拒绝。所有裁剪均写入 `scheduling_report.decisions`，包含原参数、有效参数、资源估算、业务码与理由。

## 等价性验证

每个准入任务执行两次：

- 并行：按计划批次提交到显式 `spawn` 进程池，使用配置的单批样本数分块。
- 串行基线：固定任务顺序逐组合执行，并强制使用整批 `n_rows` 作为分块大小。

因此同一次核对同时覆盖“并行 vs 串行”和“分块 vs 整体”。偏差谱包含最大/平均/P50/P95 绝对偏差、直方图、非零样本、首个分数不一致位置、首个排名不一致位置。默认声明精度为 `1e-12`。

## 质量与一致性

以 `is_injected_anomaly` 为观测真值输出：

- AUC（两两比较，支持同分 0.5 权重）
- Average Precision（同分分组）
- 固定配额检出数、检出率、精确率、召回率

评估完成后从逐样本 `score_details` 独立重算一次；不一致返回 `E_INCONSISTENT`。同时检查样本集合、重复标识、非有限分数、排名唯一性和排名顺序，并在 `details.categories` 指明类别。

## 快照与账本

`plan`、`budget`、`result` 三个 stage 各自保存完整快照：

- 单调递增 `version`
- 对 `stage + version + content` 计算的 SHA-256 `checksum`
- 每次提交整体替换当前快照，不增量修补
- 显式提交旧版本返回 `E_SNAPSHOT_ROLLBACK`
- 内容、版本或校验和被修改返回 `E_SNAPSHOT_CORRUPT`
- stage 已有在途提交时，非阻塞提交返回 `E_STAGE_BUSY`

比较预算账本使用单互斥锁完成预留与释放，`reserved`、`available`、交易状态和事件在同一临界区内更新。验证运行不计入逻辑比较预算，预算账本只记录实际待执行候选的一次调度成本。

## 主要业务码

| 场景 | 业务码 |
| --- | --- |
| 重复 `sample_id` | `E_DUPLICATE_SAMPLE_ID` |
| 特征长度不一致 | `E_FEATURE_DIM_MISMATCH` |
| 未知方法 | `E_UNKNOWN_METHOD` |
| 非正邻居数 | `E_INVALID_NEIGHBORS` |
| 非正树数/采样数 | `E_INVALID_TREES` |
| 非正预算 | `E_INVALID_BUDGET` |
| 比较预算裁剪 | `E_COMPARE_BUDGET` |
| 内存裁剪/拒绝 | `E_MEMORY_LIMIT` |
| 结果契约破坏 | `E_INCONSISTENT` |
| 快照回退 | `E_SNAPSHOT_ROLLBACK` |
| 快照损坏 | `E_SNAPSHOT_CORRUPT` |
| stage 并发占用 | `E_STAGE_BUSY` |

## 本次实测

固定种子 `20261121` 生成 600 条、5 维、6 个正态簇、48 个注入异常（8%）。

- 20 项测试全部通过。
- 基线 6 个候选全部准入，估算比较次数 `3,142,133 / 8,000,000`，峰值批次内存 `2,147,896 / 8,388,608` 字节。
- 冲突运行准入 3 个、降级 2 个、拒绝 3 个，实际账本 `428,736 / 430,000` 次比较，返回 `E_COMPARE_BUDGET` 与 `E_MEMORY_LIMIT`。
- 所有已执行任务的最大并行/串行偏差为 `0.0`，排名完全一致。
- 两次完整 CLI 输出文件 SHA-256 均为 `9235dae380fe8737cfbf30159e7b22482876742c696fdfd660630894552b7b31`，`cmp` 字节一致。
- 快照最终版本均为 2，历史保留版本 1。

详细数据见 `docs/VALIDATION.md`。
