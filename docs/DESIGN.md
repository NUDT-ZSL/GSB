# 技术设计

## 1. 数据契约

样本为 JSONL，每行包含：

```json
{
  "sample_id": "sample_000000",
  "feature": [0.1, 0.2],
  "group": "cluster_00",
  "is_injected_anomaly": false
}
```

加载后按 `sample_id` 字典序重排，所有方法使用同一顺序的 `float64` 矩阵。这样即使输入行顺序变化，方法内部也不依赖文件行序。重复标识、维度不一致、非有限数值与缺失内部标记都会在进入算法前返回结构化业务码。

`is_injected_anomaly` 只用于离线指标，不参与任何评分。

## 2. 确定性算法

### 2.1 孤立森林类随机切分

每个候选使用由全局种子和规范方法名派生的方法种子。通过 `numpy.random.Generator(numpy.random.PCG64(seed))` 依次完成：

1. 无放回子采样。
2. 从当前节点取值范围非零的特征中均匀选一个特征。
3. 在该特征当前最小/最大值之间均匀选阈值。
4. 递归构建至叶节点或最大深度。

查询时遍历每棵树得到叶深度，叶节点使用平均路径长度修正：

```text
c(n) = 2*(ln(n-1)+gamma) - 2*(n-1)/n
score(x) = 2^(-mean_path(x)/c(sample_size))
```

阈值使用 `nextafter` 防止退化为空切分。树结构、RNG 调用顺序和遍历顺序均固定。

### 2.2 KNN

对每个查询行，计算其与所有其他样本的欧氏距离，排除自身后取最近 `k` 个距离均值。实现按查询行分块，但每行使用同一组逐行 NumPy 运算，不使用依赖矩阵形状的 BLAS 归约，因此分块大小只改变外层循环边界，不改变分数。

### 2.3 马氏距离

使用样本均值与 `ddof=1` 的全协方差：

```text
cov_reg = cov + I * regularization * max(mean(diag(cov)), 1)
inv_cov = inv(cov_reg)
score(x) = sqrt((x-mu)^T inv_cov (x-mu))
```

若逆矩阵失败，记录 `pseudo_inverse_fallback`。内存预算不足时不改用一个更高维或降维近似，而是拒绝候选，以保持方法语义。

### 2.4 分数和排名

分数先量化到 12 位小数，再排序：

```text
(-score, sample_id)
```

负零统一写为 `0.0`。每个方法计算 `score_checksum = sha256(canonical({method, candidate_id, params, seed, scores, ranking}))`，用于字节级复核。

## 3. 预算估算与准入

每个候选先计算原始资源估算。内存估算组成清晰列在 `components`，比较次数估算列在 `comparisons`。预算裁决流程：

1. 对每个候选做内存准入；必要时做方法专属降级。
2. 每种方法选择一个内存安全的最高质量锚点。
3. 锚点和其余候选按配置优先级排序。
4. 按顺序尝试准入；剩余比较预算不足时，尝试参数降级。
5. 无法降级的候选以 `E_COMPARE_BUDGET` 或 `E_MEMORY_LIMIT` 拒绝。
6. 准入任务按 worker 数与批次内存双重约束确定性组批。

降级规则：

- 孤立森林内存超限：减少 `n_trees`。
- 孤立森林比较预算不足：减少 `n_trees`。
- KNN 内存超限：减少算法行分块大小。
- KNN 比较预算不足：减少 `n_neighbors`。
- 马氏距离：内存不足或比较不足均拒绝。

所有状态保存在 `scheduling_report.decisions`，接受状态为 `admitted`、`degraded` 或 `rejected`。

## 4. 并行调度

计划中保存：

- `dispatch_order`：确定性任务派发顺序。
- `execution_batches`：确定性批次边界、任务集合、估算内存与估算比较次数。
- 每个任务的 `batch_index`。
- 每个结果中的 `execution.chunks`：样本行分块边界。

执行器使用 `multiprocessing.get_context("spawn")` 和 `ProcessPoolExecutor`。worker 初始化一次后接收任务；任务异常由 worker 顶层捕获并返回结构化错误，包含业务码、阶段、方法、批次序号和任务 ID。

主进程按计划顺序收集 envelope。单个任务失败不会丢弃同批其他成功 envelope；失败任务只释放自己的预算预留。

## 5. 串并行与分块等价

引擎总是执行两套计算：

- 并行路径：按配置分块。
- 串行基线：固定顺序、强制整批分块。

每个成功任务比较样本集合、分数、排名与 `score_checksum`。偏差谱保存统计量、绝对值直方图、非零样本列表、第一个分数不一致和第一个排名不一致。任一任务不一致时报告 `E_EQUIVALENCE_MISMATCH`，但其他成功任务结果仍保留。

## 6. 质量指标

分数越高越异常。AUC 使用正负样本逐对比较，同分记 0.5。Average Precision 按同分阈值分组后积分。固定配额由 `ceil(quota_fraction*n)` 决定。

指标计算只依赖：

- `score_details`
- 内部异常标记
- 配额比例

首次计算后再从相同明细重算并比较规范化 JSON，保证指标可独立复现。

## 7. 快照与版本语义

快照对象：

```json
{
  "schema": "whole-snapshot-sha256-v1",
  "stage": "plan",
  "version": 1,
  "content": {},
  "checksum": "...",
  "checksum_algorithm": "sha256-canonical-json-v1"
}
```

校验和覆盖 `schema + stage + version + content`。提交时：

1. 非阻塞获取 stage 锁；失败立即返回 `E_STAGE_BUSY`。
2. 自动版本为当前版本 + 1；显式版本必须大于当前版本。
3. 构造并校验完整新快照。
4. 保存旧快照到只读历史副本，替换当前值。

读取当前快照时重新计算校验和，任何字段损坏返回 `E_SNAPSHOT_CORRUPT`。

预算账本使用单一 `threading.Lock`。每个 transaction 的金额、状态、序列号和总预留量随事件原子更新，快照按 transaction 序列号稳定输出。

## 8. 可复现性边界

可复现性依赖：

- 固定 Python/NumPy 环境。
- 固定样本文件。
- 固定配置与种子。
- 规范化 JSON 编码：键排序、紧凑分隔符、UTF-8、禁止 NaN。

输出不包含墙钟时间、PID、临时路径或 worker 完成顺序。CLI 的 `--check-rerun` 在进程内完整执行两遍并比较规范化 core 的 SHA-256。
