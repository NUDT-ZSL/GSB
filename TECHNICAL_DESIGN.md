# Technical Design

## 1. 模块边界

| 模块 | 职责 |
|---|---|
| `benchmark.py` | 唯一 CLI 入口 |
| `featurediag/models.py` | 任务、批次、配置和显式公式校验 |
| `featurediag/numeric.py` | 固定种子样本、z-score、量化/反量化 |
| `featurediag/aggregation.py` | 成对特征交叉、分块/整块路径和首偏差定位 |
| `featurediag/snapshot.py` | v1/v2 读取、迁移、活动快照原子提交 |
| `featurediag/cache.py` | 内容寻址缓存键和定向失效计划 |
| `featurediag/pipeline.py` | 四阶段状态机、检查点、恢复、误差账本 |
| `featurediag/generator.py` | 固定主种子输入生成 |
| `featurediag/cli.py` | 命令、业务码、验收演示 |

## 2. 任务与批次契约

任务清单每行除必需字段外，还显式声明：

```json
{
  "quant_formula": "q=round(x/scale)+zero_point",
  "dequant_formula": "x_hat=(q-zero_point)*scale",
  "rounding_mode": "half_away_from_zero",
  "normalization": {"method": "zscore", "ddof": 0, "epsilon": 1e-12},
  "reduction_rule": "canonical_block_order_exact",
  "feature_ids": ["f0", "f1"],
  "tolerance": {"abs_max": 2.0, "rel_max": 0.15}
}
```

`block_plan` 支持两种等价写法：

- 边界写法：`[0, 3, 6, 8]` 表示 `[0,3)`, `[3,6)`, `[6,8)`
- 显式块写法：`[[0,2], [2,5], [5,6]]`

显式块必须无重叠、无空洞地覆盖 `[0, feature_dim)`。

样本矩阵种子为：

```python
SeedSequence([stable_int(task_id), batch.feature_seed])
PCG64 -> Generator.standard_normal(dtype=float64)
```

批次流读入后按 `batch_id` 升序处理。重复 `batch_id`、缺失 `batch_id`/`task_id`、未知任务、非法 `sample_count` 或 `feature_seed` 均返回 `E_BAD_BATCH`。

## 3. 归一化与定点路径

归一化使用 `math.fsum` 逐列计算均值和方差，避免 NumPy 归约顺序带来的平台差异。统计量包括 `mean`、`std`、`denominator`、`ddof`、`epsilon` 和统计量哈希，写入阶段 0 载荷及最终误差账本。

同一 task/batch 的 float64 路径与定点路径只生成一次归一化数组。阶段 1 从已提交的归一化 `.npy` 产物读取，并使用其 `stats_hash`；不会在定点路径中重新估计均值或方差。

量化前检查所有输入是否为有限 float64。发现非有限值时，错误上下文记录非有限数量和首个 `[sample, feature]` 位置，返回 `E_QUANT_INPUT`。量化与反量化输出均校验声明形状，不符时返回 `E_QUANT_LAYER`。

## 4. 分块聚合

每个 block 的输出是块内全部无序特征对 `(i, j), i <= j` 的逐样本乘积之和：

```python
math.fsum(float(z[s, i] * z[s, j]) for s in samples)
```

每个输出特征对只属于一个 block。跨 block 的归约只负责按显式规则组合互相独立的块输出，因此不会引入额外浮点加法。`math.fsum` 的正确舍入精确累加使分块路径与整块路径逐位相同。

聚合阶段同时计算：

- float64 整块参考值 `reference`
- 定点反量化后的分块值 `candidate`
- 定点反量化后的整块值，用于严格一致性比较

若任一路径逐元素不一致，抛出 `E_BLOCK_MISMATCH`，并报告首个偏差的特征对、块号、块内位置、全局输出位置和两侧数值。

## 5. 误差谱

四级指标均包含：

- `abs_max` / `abs_deviation`
- `abs_rms`、`abs_mean`
- `rel_max` / `rel_deviation`
- `rel_rms`
- `amplification_max` / `error_amplification_ratio`
- `amplification_rms`
- `reference_rms`

各阶段参考口径：

| 阶段 | 候选 | 参考 |
|---|---|---|
| `sample_normalization` | 持久化归一化数组 | 从原始矩阵和同一统计量重算的数组 |
| `fixed_point` | 反量化数组 | float64 归一化数组 |
| `block_aggregation` | 定点分块聚合 | float64 整块聚合 |
| `error_spectrum` | 最终残差的谱摘要 | 零残差参考 |

相对偏差以参考数组的 RMS 作为尺度，避免接近零的元素把相对误差放大到无业务意义的值。`dominant_level` 由各阶段新引入的相对 RMS 误差决定。

容差同时检查定点阶段和分块阶段的绝对最大偏差与相对 RMS 偏差。超过任一阈值时将当前账本条目标为 `failed`，写入 `E_TOLERANCE_EXCEEDED`，但继续处理其他批次/任务；最终 CLI 以退出码 3 返回，已完成账本不丢弃。

## 6. 快照格式与迁移

v1 单文件：

- `format_version: "v1"`
- `epoch_ms`、`feature_generation`
- `entries[]`：`feature_id`、相对 `effective_seconds`、`base_params`
- 可选 `invalid_features[]`：`feature_id`、相对 `effective_seconds`、`generation`

v2：

- `manifest.json` 是版本头、分片清单、逻辑时钟和内容校验和
- `shard-*.json` 保存特征基础参数
- `metadata.sqlite` 的 `features` 表保存绝对 `effective_ms`、`generation`、分片归属
- `tombstones` 表保存失效墓碑的绝对毫秒、代数和原因

读取前先按版本头分派。未知版本返回 `E_VERSION_UNSUPPORTED`；缺字段、重复特征、跨分片/数据库不一致、校验和不符、生效时间晚于快照时钟或时间戳倒退返回 `E_FORMAT_CORRUPT`。

v1 -> v2：

- `absolute_ms = epoch_ms + round(effective_seconds * 1000)`
- 每个特征补全 `generation`，来源写入 `migration_provenance`
- `invalid_features` 补全为墓碑；原因缺失时使用显式默认值并记录来源

v2 -> v1：

- 绝对毫秒转换为相对秒，重新读取后验证毫秒边界不变
- 墓碑的完整原因无法进入 v1，按规则只保留失效特征 ID/时间/代数，原因写入迁移报告并明确丢弃
- 失效特征不进入 v1 活动表，因此活动集合和失效集合均保持不变

迁移函数在返回前重新读取目标并逐项比较活动集合、失效集合和毫秒边界。

## 7. 活动快照提交

`artifacts/snapshots/active_snapshot.json` 保存完整规范化快照。提交使用：

1. `fcntl.flock(LOCK_EX | LOCK_NB)` 获取单写者锁
2. 比较已生效与来件版本、逻辑时钟和内容校验和
3. 版本或校验和变化时构造完整新文件
4. 临时文件 `fsync`，`os.replace` 原子替换

同一版本但不同校验和允许整替换；版本倒退返回 `E_SNAPSHOT_ROLLBACK`，逻辑时钟倒退返回 `E_CLOCK_ROLLBACK`。锁冲突立即返回 `E_STAGE_BUSY`，不等待。

## 8. 缓存键与定向失效

阶段键采用渐进依赖，避免无关变更污染上游：

| 阶段 | 主要键内容 |
|---|---|
| `normalize` | task/batch、原始样本内容哈希、归一化配置、快照版本 |
| `quantize` | 原始样本哈希、归一化产物哈希、量化参数、公式、舍入 |
| `aggregate` | 原始样本哈希、量化参数、block_plan、归约规则、规范版本、任务特征规范摘要、反量化产物哈希 |
| `spectrum` | 原始样本哈希、量化参数、block_plan、规范版本、修订号、容差、聚合产物哈希 |

最终 `spectrum` 键覆盖需求要求的全部输入维度；账本还保存 `cache_inputs`，逐项展示原始哈希、量化参数、分块、规范版本和上游哈希。

失效计划由以下变化触发：

- task `revision` 提升
- `block_plan` 改变
- v2 特征 `generation` 或基础参数改变
- 量化参数或归一化配置改变

计划区分 `reuse_manifest` 与 `recompute_manifest`。内容键未变且旧产物已提交时复用；首个变化的阶段及其全部下游后缀重算。演示中的修订场景实际产生 12 次复用、4 次重算；`task-beta` 四阶段全部复用，`task-alpha` 仅交叉聚合与误差谱后缀重算。

## 9. 检查点与恢复

阶段名固定为：

```text
normalize -> quantize -> aggregate -> spectrum
```

每阶段只在全部批次都处理完后提交：

1. 在 `.stage_XX.vV.tmp/` 写入阶段数组、载荷和错误记录
2. 将临时目录原子改名为 `stage_XX.vV/`
3. 写 `checkpoints/stage_X.json`，包含严格递增版本、全部 `completed_batches`、`complete=true`、载荷校验和
4. 更新 `state.json`
5. 发布内容寻址缓存记录

启动恢复时：

- 从 `stage_0` 起验证连续检查点、版本单调性、批次数集合和载荷校验和
- 删除 `.stage_*.tmp` 和未被检查点引用的阶段目录
- 从最近已提交检查点的下一阶段起，从该阶段起点重建全部批次
- 已提交前缀不重算；未提交中间文件不参与恢复

`verify` 命令重新验证四个检查点、每个数组产物哈希、账本行序列化校验和与物化账本文件。

## 10. 边界与约束

- 无网络、无外部模型、无 Docker
- 固定 NumPy 2.0.0；样本字节与统计量哈希可复现
- 仅使用本地确定性输入
- v1 的 `invalid_features` 是保留失效集合/边界所需的扩展字段；墓碑原因按显式规则降级丢弃
