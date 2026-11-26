# 多格式稀疏真值账本实现说明

## 1. 快速运行

环境要求：Python 3.10+、NumPy 1.24+；v2 位图使用标准库 `sqlite3`，测试使用 `pytest`。

```bash
python3 -m pip install -r requirements.txt
bash scripts/verify.sh
```

常用命令：

```bash
PYTHONPATH=. python3 -m pytest -q
PYTHONPATH=. python3 -m truth_ledger.cli generate --out samples --cases 2
PYTHONPATH=. python3 -m truth_ledger.cli demo --root artifacts/demo
PYTHONPATH=. python3 -m truth_ledger.cli adjudicate --case-dir artifacts/demo/normal-run/case-001
PYTHONPATH=. python3 -m truth_ledger.cli self-check --case-dir artifacts/demo/normal-run/case-001
PYTHONPATH=. python3 -m truth_ledger.cli inject --out artifacts/injections
```

也可安装为命令：`python3 -m pip install -e .`，之后直接运行 `truth-ledger demo`。

## 2. 代码结构

- `truth_ledger/generator.py`：固定 `np.random.RandomState(seed)` 生成 CSR、SpMM 稠密右矩阵、SpMSpM 稀疏右矩阵和确定性写入事件。
- `truth_ledger/ingest.py`：CSR 结构校验，违规统一为 `E_STRUCT`。
- `truth_ledger/format_v1.py`：v1 单文件 JSON，四字段行优先扁平表。
- `truth_ledger/format_v2.py`：v2 分片 JSON 与 SQLite `bitmap_index` 位图索引。
- `truth_ledger/format_v3.py`：v3 列块描述，载荷使用 zlib + Base64 压缩并记录列块映射。
- `truth_ledger/migrate.py`：以 canonical CSR 为枢轴完成三格式互迁、补全来源和显式丢弃规则。
- `truth_ledger/writes.py`：写入版本、哈希、区间重叠校验、按起始行应用和失效区间映射。
- `truth_ledger/cache.py` / `recompute.py`：内容寻址缓存、键派生、区间复用/重算。
- `truth_ledger/ops.py`：固定累加顺序的 float64 SpMM 与 SpMSpM。
- `truth_ledger/adjudicate.py`：只从磁盘读取账本、RHS、lineage 和缓存键，重新执行并裁决三视图。
- `truth_ledger/state.py` / `pipeline.py`：四阶段快照状态机、原子替换、SIGKILL 恢复和 GC。
- `truth_ledger/selfcheck.py`：快照集合、覆盖分区、哈希复现、增量与全量一致性。
- `truth_ledger/injections.py`：四阶段异常注入和业务码断言。
- `tests/test_truth_ledger.py`：15 个验收测试，包括真实子进程 SIGKILL 恢复和未提交写入回滚。

## 3. 账本结构

统一 CSR 包含：

- `row_ptr: int64[nrows+1]`
- `col_idx: int64[nnz]`
- `values: float64[nnz]`
- `timestamps: int64[nnz]`
- `case_id`、`seed`、`config`、`write_version`

摘要是对带长度前缀的小端数组字节计算 SHA-256，避免 JSON 格式、空白或字段顺序影响结构哈希。

### v1

`flat_table` 每条记录固定为：

```text
[row_index, col_idx, value, timestamp]
```

记录字段数不是 4 返回 `E_FORMAT`；必填顶层字段缺失返回 `E_FIELD_MISSING`；数字不可解析返回 `E_PARSE`。

### v2

目录包含 `v2_manifest.json`、`shard_0000.json` 等分片和 `bitmap_index.sqlite`。SQLite 表保存每个分片的：

```text
case_id, shard_id, row_start, row_end, row_count, bitmap, bitmap_digest
```

分片空洞、重叠或越界返回 `E_SHARD_BOUND`；位图短于覆盖要求、缺少行位、设置分片外 padding 位或摘要不符返回 `E_BITMAP_COVER`。

### v3

按 `config.block_cols` 切列块。每个 block 保存 `col_start/col_end`、全局列映射公式、压缩载荷和摘要。载荷包含 `row_idx/local_col_idx/values/timestamps`。另保存非空列块位图与结构代数默认值：

```text
addition=float64_ordered_sum
multiplication=float64_product
zero=0.0
order=row -> ascending col -> upstream sparse row
```

## 4. 迁移与语义漂移

所有迁移都经过 canonical CSR，并在写出后重新读取目标格式，与源 CSR/期望 CSR 逐项比较：

1. shape / nrows / ncols
2. 每行 NNZ 与 `row_ptr`
3. 列索引
4. float64 值
5. 时间戳

发现差异时抛出 `E_SEMANTIC_DRIFT`，`details.first_diff` 给出 `kind/row/col/position` 以及左右值。

v1 -> v2 记录分片边界和位图摘要来源；v1 -> v3 记录列块映射、结构代数默认值和块位图摘要来源。v3 -> v1 的报告显式丢弃：

- 列块 codec/payload/mapping 压缩描述；
- 列块位图与 `bitmap_validation` 元数据。

## 5. 写入、失效与缓存

写入事件示例：

```json
{
  "write_version": 2,
  "writes": [
    {
      "row_start": 6,
      "row_end": 9,
      "row_ptr": [0, 2, 5, 7],
      "col_idx": [2, 8, 1, 4, 9, 0, 7],
      "values": [],
      "timestamps": [],
      "content_hash": "sha256:..."
    }
  ]
}
```

版本回退、批次内区间重叠/重复、内容哈希不符均整体拒绝为 `E_WRITE_CONFLICT`。合法批次按 `row_start` 排序应用；receipt 输出写入区间、映射到的标准行区间、`col_min/col_max+1` 和触发版本。

标准行区间由 `config.interval_rows` 决定，默认是：

```text
R00:0-3, R01:3-6, R02:6-9, R03:9-12
```

缓存键包含：

- 区间 CSR 片段摘要；
- 区间最后写入版本（未改动区间保持旧版本，因此不会被全局批次号误失效）；
- `format_source=v1|v2|v3`；
- SpMM 稠密右矩阵摘要；
- SpMSpM 稀疏右矩阵摘要；
- 该区间引用到的所有上游稀疏行片段哈希。

当前键集合写入 `cache/cache_manifest.json`。阶段 3 提交后 GC 非当前对象，实现旧产物替换。

## 6. 四阶段状态机与强制终止

快照位于 `state/snapshots/`，`state/latest.json` 通过临时文件 + `os.replace` 原子替换。快照包含单调递增 `snapshot_version`、阶段、写入版本、全部/已完成区间、区间集合哈希、CSR 摘要、当前缓存键和内容校验和。

阶段：

1. `parse_validate`
2. `migrate_semantic`
3. `incremental_recompute`
4. `result_summary`

重启时：

- 读取最近快照；
- 将缓存 manifest 对账到该快照；
- 删除未被快照引用的缓存对象/片段；
- 清空 `state/work/`；
- 已提交阶段标记为 `skipped_committed`，不重复执行或计入；
- 从最近未完成阶段起点重建。

演示使用 `--crash-at recompute_after_first_new` 在第一个新区间写入缓存对象后向自身发送 `SIGKILL`。子进程返回 `-9`，最近快照停在版本 6（阶段 2）；恢复时跳过阶段 1/2，删除 1 个未提交对象和 1 个片段，然后完整重算阶段 3/4。

## 7. 跨视图裁决与自检

`adjudicate_from_disk(case_dir)` 不接收旧矩阵或运算结果对象，只从以下磁盘内容复原：

- `committed/ledgers/v1_ledger.json`
- `committed/ledgers/v2/`
- `committed/ledgers/v3_ledger.json`
- `rhs_dense_b.npy`、`rhs_sparse.json`
- `committed/lineage.json`
- `cache/objects/*.json`

裁决器重新执行区间 SpMM/SpMSpM，校验缓存输出和中间贡献摘要，再比较三视图的 CSR 结构、时间戳、SpMM、SpMSpM 和贡献哈希。结构/时间戳问题返回 `E_STRUCT_VIEW`，数值或缓存哈希问题返回 `E_CROSS_VIEW_MISMATCH`。

自检还会把缓存中的区间产物拼装为完整矩阵，与一次整体重建比较。默认样本的累加顺序固定，因此 float64 下 `np.array_equal` 逐元素一致，最大绝对差为 `0.0`。

## 8. 错误码

| 场景 | 业务码 |
| --- | --- |
| CSR 指针、边界、排序、NNZ 等违规 | `E_STRUCT` |
| 版本不在 1/2/3 | `E_VERSION_UNSUPPORTED` |
| v1 字段数、JSON 结构、分片对齐错误 | `E_FORMAT` |
| 同行列索引重复 | `E_INDEX_DUP` |
| v2 分片边界越界/空洞/重叠 | `E_SHARD_BOUND` |
| SQLite 位图缺失、覆盖不足或越界位 | `E_BITMAP_COVER` |
| 必填字段缺失 | `E_FIELD_MISSING` |
| JSON/数字/压缩载荷不可解析 | `E_PARSE` |
| 迁移后语义不一致 | `E_SEMANTIC_DRIFT` |
| 写入版本、重叠、哈希冲突 | `E_WRITE_CONFLICT` |
| 三视图结构/时间戳不一致 | `E_STRUCT_VIEW` |
| 三视图数值/缓存哈希不一致 | `E_CROSS_VIEW_MISMATCH` |
| 覆盖、恢复或复现自检失败 | `E_INCONSISTENT` |
