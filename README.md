# 林区巡护日志基数估计内核

这是一个完全离线、固定种子可复现的基数估计工作台。它把巡护员、终端、路线、区域、频段、基站和逐条巡护日志装入 SQLite，依据等深直方图、采样行数和区域频段组合统计估算连接结点基数，并用逐行真实连接扫描核对偏差。

实现只使用 Python 3 标准库和 NumPy，不依赖线上数据或外部数据库服务。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m patrol_stats demo
python3 -m unittest discover -s tests -v
```

`demo` 会在 `storage/` 下创建：

- `storage/patrol_stats.db`：SQLite 数据、统计摘要、评估快照和更新计划。
- `storage/generated/*.csv`：确定性生成的基表、查询和 `manifest.json`。
- `storage/reports/`：摘要校验、初版评估、更新计划、更新后评估和版本对比 JSON。

默认种子为 `20261128`，生成 5000 条日志和 16 条查询。相同参数重复运行会生成逐字节一致的 CSV 与一致的 `data_sha256`。

## 目录结构

```text
patrol_stats/
  schema.sql       SQLite 基表、摘要表和快照表
  generator.py     偏斜巡护数据与查询的确定性生成器
  statistics.py    等深直方图、采样、组合统计、摘要校验
  estimation.py    单谓词选择率、连接结点基数、逐行真实扫描
  optimizer.py     区域/频段/时间过滤索引与连接顺序代价
  evaluation.py    工作负载评估、SQLite 快照、版本回看
  advisor.py       偏差归因、摘要更新建议与应用收敛核验
  cli.py           命令行入口
tests/
  fixtures.py      手工构造的 12 条日志小型数据集
  test_histogram.py
  test_manual_estimation.py
  test_determinism.py
  test_end_to_end.py
```

## 数据模型

基表：

- `regions`、`bands`、`rangers`、`terminals`、`routes`、`base_stations`
- `patrol_logs`：每条日志记录巡护员、终端、路线、区域、频段、基站、时间和信号指标
- `queries`：固定种子生成的区域、频段和时间窗口组合

生成器为每个“区域 × 频段”组合铺设少量覆盖记录，再按热点区域高权重、偏远区域低权重生成剩余日志。热点组合密集、偏远组合稀疏，但每条查询都保证至少有少量真实命中，便于比较相对偏差。

## 估计口径

### 1. 摘要校验

`validate_summary()` 逐项检查：

- `stat_row_counts` 中的基表行数与当前 `COUNT(*)`。
- 直方图记录的总行数、NULL 数、桶计数之和。
- 每个桶的摘要计数与按桶边界重算的基表计数。
- 区域频段组合摘要的总行数和全量桶计数。

失效项写入带 `table_name`、`column_name`、`reason` 的报告，`python3 -m patrol_stats validate` 在失效时返回非零退出码。

### 2. 单谓词选择率

`patrol_logs.region_id`、`band_id`、`logged_at` 使用等深直方图。桶边界用 NumPy 排序、分位切片和 `searchsorted` 计算；重复边界产生独立非空桶，空桶被压缩。

等值谓词输出命中桶、桶计数、桶内离散值跨度和均匀部分覆盖比例。范围谓词逐桶计算：

```text
bucket_contribution = bucket_count / total_rows
                    * overlap_width / bucket_width
```

最终选择率把直方图值与采样值混合：

```text
eq:    P = bucket_share * [(1-w) * uniform_fraction
                          + w * smoothed_sample_fraction]
range: P = (1-w) * histogram_P + w * sample_P
```

当采样预期命中不足 8 行时，`w=0`；否则 `w` 有上限，避免小样本噪声完全覆盖直方图。每个谓词均输出 `hit_buckets`、`uniform_partial_fraction`、`sample_selectivity`、`final_selectivity` 和公式。

### 3. 联合选择率与结点基数

同一区域、频段、时间查询同时计算两种口径：

```text
independence = P(region) * P(band) * P(time)
combo        = P(region, band from joint histogram) * P(time)
base         = fact_rows * joint_selectivity
```

结点基数沿连接链逐层修正：

```text
N_i = N_{i-1} * containment * right_multiplicity
containment        = overlap_ndv / left_ndv
right_multiplicity = right_rows / right_ndv
```

生成数据中终端连接只接受 `terminals.active = 1`，因此终端键包含率小于 1，可以在快照中观察结点基数下降。每个结点同时保存独立性估计、组合直方图估计、实际行数、偏差、口径标记和修正原因。

### 4. 连接顺序与索引建议

优化器枚举六个维表连接顺序，并以过滤后基数、维表行数、键值域包含率和右表多重度计算代价；输出推荐顺序、基线和前五个候选。

索引候选逐条保存接受/拒绝理由，包括：

- `patrol_logs(region_id|band_id, band_id|region_id, logged_at)` 复合过滤索引。
- 单列 `logged_at` 索引。
- 事实表外键列索引。
- 终端频段/区域索引。

复合索引是否接受由过滤后行数占基表比例和扫描/随机探测代价决定。

### 5. 真实行数与偏差

每条查询按连接顺序加载事实行，再逐行检查右表键域并累计多重度，得到每个结点的真实行数。相对偏差为：

```text
relative_error = abs(estimate - actual) / max(actual, 1)
```

报告给出最终真实行数、逐结点实际行数、平均/最大偏差以及首个超过查询阈值的结点 ID。

### 6. 摘要更新与收敛

`advisor.py` 对偏离的单列谓词和区域频段组合逐项归因：

- 单列平均/最大偏差过高：建议重建该列等深直方图。
- 组合统计来自小样本、组合缺失或偏差过高：建议全量构建区域频段组合直方图。
- 摘要校验失效：附带元数据修复建议。

应用计划时创建新摘要版本，全量重算摘要和组合统计，但旧版本快照保留不变；随后重跑同一批查询并比较更新前后平均偏差。满足平均偏差下降至少 5% 或绝对值不高于 0.05 时标记为收敛。

## 常用命令

```bash
# 一键演示、更新和收敛核验
python3 -m patrol_stats demo

# 分步执行
python3 -m patrol_stats generate --seed 20261128 --logs 5000 --queries 16
python3 -m patrol_stats stats --version v1_initial \
  --sample-size 512 --combo-sample-size 160 --combo-source sample
python3 -m patrol_stats validate --version v1_initial
python3 -m patrol_stats evaluate --version v1_initial \
  --output storage/reports/evaluation_v1_initial.json
python3 -m patrol_stats advise --version v1_initial --apply
python3 -m patrol_stats snapshots
python3 -m patrol_stats snapshot --run-id 1
python3 -m patrol_stats compare --query-id Q001 \
  --from-version v1_initial --to-version v2_updated
```

使用其他数据库目录：

```bash
python3 -m patrol_stats --db /tmp/patrol/demo.db demo
```

## 快照表

- `stat_versions`、`stat_row_counts`、`stat_column_stats`
- `stat_samples`、`stat_histograms`、`stat_histogram_buckets`
- `stat_combo_histograms`、`stat_combo_buckets`
- `stat_join_domains`
- `evaluation_runs`、`query_evaluations`、`node_estimates`
- `join_order_recommendations`、`index_recommendations`
- `advisor_suggestions`、`update_plans`

`query_evaluations` 保存摘要版本、谓词详情、估计底座、真实行数和首个越阈结点；`node_estimates` 保存每个连接结点的两套估计、选定估计、真实行数、重叠率、多重度、偏差原因和计算明细。

## 手工验收

`tests/fixtures.py` 手工插入 12 条日志，其中区域一/频段一计数为 5，但单列合计分别为区域一 6、频段一 7。因此：

```text
独立性区域频段估计 = 12 * (6/12) * (7/12) = 3.5
组合直方图估计     = 5
```

测试选择一个可被等深桶精确隔离的稀疏组合，验证组合估计、逐结点累计和逐行扫描真实值；另有人工破坏行数/桶计数的摘要失效测试、固定种子字节一致性测试，以及“初版摘要 → 建议 → 更新 → 同批查询收敛”的端到端测试。

## 口径与限制

- 组合直方图只建模区域与频段的联合频率；时间仍作为独立谓词，查询量极大时可扩展为三维组合摘要。
- 估计值保留浮点行数；真实行数是整数扫描结果。
- 采样混合权重有上限，适合演示可解释性与摘要更新，不宣称替代生产优化器的完整代价模型。
- 所有生成、评估和更新记录均写入本地 SQLite；测试和演示不读取任何网络数据。
