# 稀疏矩阵乘内核工作台

这是一个完全离线、可复算的 SpMV（稀疏矩阵 × 稠密向量）工作台，用于：

- 读取 Matrix Market、CSR 文本、CSC 文本三种稀疏片段；
- 按版本声明归一字段名、索引基数和权重单位；
- 根据运算方向和行列非零分布选择 CSR/CSC；
- 以“完整行不拆分”为硬约束分批；
- 逐批执行内核并记录扫描量、缓存命中、结果增量；
- 用独立稠密 `A @ x` 产出真值并逐元素核验；
- 比较两个布局的结果和账目，给出可复现推荐或冲突拒绝；
- 核对三个版本的矩阵、向量、结果、账本与差异清单。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 -m sparse_kernel_workbench validate
python3 -m sparse_kernel_workbench run
python3 -m unittest discover -s tests -v
```

默认报告写到 `reports/run_report.json`。仓库已附带一次完整样本运行结果
`reports/sample_run.json`。

不写文件、只做完整校验：

```bash
python3 -m sparse_kernel_workbench validate
```

把运算方向改成按列累加，验证格式选择不是固定输出：

```bash
python3 -m sparse_kernel_workbench validate --direction column_accumulate
```

也可以显式传入版本规格与输出文件：

```bash
python3 -m sparse_kernel_workbench run \
  --operation data/operation_spec.json \
  --version-spec data/versions/v1_matrix_market_1based/version.json \
  --version-spec data/versions/v2_csr_0based_permille/version.json \
  --version-spec data/versions/v3_csc_1based_basis_points/version.json \
  --output reports/run_report.json
```

## 仓库内容

```text
data/
  operation_spec.json
  versions/
    v1_matrix_market_1based/
    v2_csr_0based_permille/
    v3_csc_1based_basis_points/
sparse_kernel_workbench/
  parsers.py       三种格式解析
  normalize.py     索引/字段/权重归一与稠密向量归一
  distribution.py  行列非零统计与格式选择
  batching.py      完整行硬约束分批
  kernel.py        逐批 SpMV 与运行账目
  reference.py     独立稠密参考、误差直方图
  audit.py         策略推荐与跨版本审计
  pipeline.py      端到端流程、原子输出
  cli.py           命令行入口
tests/
  test_workbench.py
tools/
  generate_fixtures.py  确定性样本生成器
```

## 附带样本

三个版本描述同一张逻辑矩阵：

| 版本 | 格式 | 索引 | 原始权重单位 | 换算因子 | 最高声明索引 |
|---|---|---|---:|---:|---|
| `v1-matrix-market-1based-count` | Matrix Market | 1-based | count | `1` | row 48, col 24 |
| `v2-csr-0based-permille` | CSR text | 0-based | permille | `0.001` | row 47, col 23 |
| `v3-csc-1based-basis-points` | CSC text | 1-based | basis point | `0.0001` | row 48, col 24 |

归一后矩阵为 `48 × 24`，88 个非零，密度 `88 / (48 × 24) = 7.64%`：

- 第 0 行有 24 个非零，是单行极密行；
- 第 0 列有 24 个非零，是单列极密列；
- 空行为 `[12, 17, 22, 29, 32, 33]`；
- 最高声明索引在三个版本中都真实出现，且归一为同一坐标。

CSR/CSC 文本使用“标签 + 数组”形式，标签名由版本规格的 `fields` 映射。
例如：

```text
format csr
user_count 48
item_count 24
interaction_count 88
user_row_ptr 0 24 ...
user_ids 0 1 ...
item_indices 0 1 ...
weight_permille 6000 2000 ...
```

`row_ptr`/`column_ptr` 始终是零基存储偏移；`index_base` 只作用于显式的行、列
坐标 ID。这一区分在混合使用 1-based 坐标和压缩指针时非常重要。

## 归一契约

统一模型：

- 行、列位置均为零基；
- 权重统一为原始 count；
- 重复坐标在归一阶段按“求和一次”合并；
- 矩阵维度公式为
  `dimension = highest_declared_index - index_base + 1`；
- 稠密兴趣向量也通过声明字段与索引基数归一到同一列坐标。

每个版本的归一报告包含：

- `index_base`、`index_offset_to_zero`；
- `weight_unit`、`weight_conversion_factor`；
- 原始/归一矩阵形状与非零数；
- 空行、空列；
- 字段名、索引偏移、权重换算、维度基准等逐条 `field_corrections`；
- 源矩阵、版本规格、稠密向量的 SHA-256。

跨版本审计会比较归一矩阵哈希、稠密向量、每个布局的结果向量、完整运行账本、
空行和推荐布局。`cross_version.differences` 汇总所有版本被修正的字段名、索引
偏移和权重因子。

## 格式选择

`nonzero_distribution` 输出完整的行/列非零计数数组和可复算统计：

- 非零总数、活跃轴数、空轴数；
- 最大非零轴及位置；
- 活跃轴均值、最大值/均值比；
- 全轴均值、标准差、变异系数；
- 活跃轴 P90。

格式选择以运算方向为主输入：

- `row_accumulate` → 选 `csr`；
- `column_accumulate` → 选 `csc`。

另一个布局仍会实际执行，作为结果和账目的对照。报告中同时保留两个方向的
分布统计与选择理由，因此修改 `--direction` 会改变 `layout_selection.selected`。

## 分批与内核

### 完整行硬约束

所有布局都按完整行分批。禁拆规则优先于预算：

- 普通批累计非零数不超过 `batch_nonzero_budget`；
- 若单行非零数超过预算，该行单独成批并标记
  `single_row_exceeds_batch_budget_kept_whole`；
- 空行保留在其所属行区间，同时出现在全局 `empty_rows` 和批级
  `empty_rows`；
- 结果行所有权数组必须全为 `1`：每个结果元素恰好由一个批贡献。

每批输出：

- `row_interval`：`[start, end)`；
- `canonical_nonzero_interval`：行主序规范坐标中的 `[start, end)`；
- `storage_scan_ranges`：选定存储布局中的连续扫描区间，CSC 可能有多个；
- 空行、是否超预算、原因。

### 稠密向量分块复用

每批使用自己的局部稠密向量块缓存：

- 每访问一个非零，命中其列所在块；
- 批内第一次访问该块记为 miss，后续访问记为 hit；
- 每块 `hits = accesses - 1`；
- 下一批重新建立缓存，因此跨批不会虚假累计命中。

账本中的 `dense_chunk_hits` 是各批命中数之和，每批还带完整
`dense_vector_chunk_reuse` 表。

### 扫描与热点

账本包含：

- `nonzero_scan_total`；
- `batch_count`；
- `mean_nonzeros_per_result_element = nonzero_scan_total / result_vector_length`；
- `mean_nonzeros_per_batch`；
- `hotspot_multiplier` 与 `hotspot_threshold`；
- `hotspot_batch_ids`、`hotspot_scan_total`、`peak_batch_nonzeros`。

热点定义由运算规格声明，当前工作台使用：

```text
mean_nonzeros_per_batch = nonzero_scan_total / batch_count
hotspot_threshold = mean_nonzeros_per_batch × hotspot_multiplier
batch is hot iff batch_nonzeros > hotspot_threshold
```

`hotspot_average_basis` 固定写入 `per_batch`，避免把“单位结果元素平均扫描量”
误用为批次热点阈值。

同一结果元素的贡献不会重复计入：每个批拥有完整的行区间，内核另外核对
“每个规范非零只扫描一次”和“每个结果行只被一个批拥有”。

## 独立真值与误差

参考实现不使用 CSR/CSC 内核，而是：

1. 在归一坐标上构造 NumPy 稠密矩阵；
2. 使用归一后的稠密兴趣向量；
3. 计算 `dense @ interest`。

每个策略输出：

- `max_abs_error`；
- 8 桶误差直方图 `error_histogram`；
- 超出容差的 `exceeded_elements`；
- 全部结果元素的绝对误差；
- 容差（来自 `operation_spec.json`）与是否通过。

样本最大绝对误差约为 `2.84e-14`，容差为 `1e-9`。

## 推荐与冲突

两个布局都会执行。推荐顺序为：

1. 非零扫描总数；
2. 热点批次数；
3. 热点批扫描量；
4. 最大单批扫描量；
5. 批次数；
6. 是否匹配方向选择的布局；
7. 稠密块命中数（降序）。

任一策略超出独立真值容差，或两个策略结果彼此不一致时：

- `recommendation` 为 `null`；
- `recommendation_refused` 为 `true`；
- 报告给出 `conflict` 与逐元素不一致位置；
- 不输出任何推荐配置。

样本行方向推荐 `csr`，列方向推荐 `csc`。样本两布局的扫描量和热点完全同构，
因此方向匹配作为决胜证据出现，而不是硬编码结果。

## 样本运行结论

`data/operation_spec.json` 声明：

- 方向：`row_accumulate`；
- 批预算：12 个非零；
- 稠密块：4 列；
- 热点倍数：1.5；
- 容差：`1e-9`；
- 策略：`csr`、`csc`。

三个版本归一后均得到：

- 7 个批，扫描非零总数 88；
- 批扫描量 `[24, 12, 12, 12, 12, 12, 4]`；
- 单位结果元素平均扫描量 `88 / 48 = 1.8333...`；
- 热点阈值 `18.8571...`，仅第 0 批热点；
- 6 个空行显式记录；
- 两个策略结果一致，跨版本结果和账本哈希一致；
- 推荐 `csr`。

## 错误码与原子输出

关键业务错误码：

| 错误码 | 含义 |
|---|---|
| `SKW-IO-*` | 文件缺失、JSON 非法、输出失败 |
| `SKW-SPEC-*` | 版本/运算规格字段非法 |
| `SKW-FMT-*` | 片段格式、计数、指针、数组长度非法 |
| `SKW-NORM-002` | 行/列索引越界 |
| `SKW-NORM-003` | 权重换算失败 |
| `SKW-NORM-004` | 文件维度与声明最高索引不一致 |
| `SKW-NORM-VEC-*` | 稠密向量字段、坐标、覆盖范围非法 |

错误 JSON 包含 `code`、`stage`、`message`，以及可定位上下文：
`version`、`file`、`line`、`element`、`field`、`raw_value`、`reason`。

运行流程先完成所有版本的解析、归一、计算与审计，最后才通过临时文件 +
`os.replace` 原子写入报告。任一版本归一失败时：

- 返回非零退出码；
- 错误 JSON 写到 stderr；
- 不创建或覆盖目标报告文件；
- 不留下部分结果。

退出码：

- `0`：成功；
- `2`：输入、归一或输出业务错误；
- `3`：策略冲突或跨版本冲突，完整冲突报告会写出但推荐被拒绝。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 三版本归一坐标和权重一致；
- 1-based/0-based 偏移与三个权重因子；
- 单行 24 非零不拆批、6 个空行显式记录；
- 88 个非零各扫描一次、每个结果行只属于一个批；
- 两个布局的账本与结果对比；
- 独立稠密真值、误差直方图、容差；
- 行/列方向动态选择 CSR/CSC；
- 跨版本账本哈希一致；
- 坏索引业务错误且目标文件不存在；
- 跨版本权重不一致时拒绝推荐；
- 策略结果冲突时拒绝推荐；
- 原子成功输出。

## 边界

- 仅支持 Matrix Market `coordinate general`，不做网络访问。
- CSR/CSC 文本是标签数组格式，解析器按版本规格映射标签。
- 为保证“单行极密不拆分”，所有布局都采用完整行分批；CSC 的列局部性体现在
  批内扫描顺序和存储区间，而不是牺牲行完整性。
- 工作台测量的是确定性扫描量和分块复用账目，不做墙钟性能基准或依赖机器噪声。
