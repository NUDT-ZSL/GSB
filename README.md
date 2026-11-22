# 增量 CSR 稀疏矩阵乘法组件

本工作区交付一个可本地集成的 Python 组件，用于确定性 CSR 样本上的 SpMM、SpMSpM、版本化行区间写入、增量重算、一致性自检、快照替换与 SIGKILL 后恢复。样本完全由固定 seed 在本地生成，不依赖外部数据集、容器或服务。

## 交付内容

- `sparse_inc/`：核心包，暴露 `spmm`、`spspmm`、`execute_case`、`SparseIncrementalService`、写入提交、自检与快照入口。
- `scripts/generate_samples.py`：生成确定性 JSONL 样本与写入批次。
- `scripts/recovery_demo.py`：执行一次真实 SIGKILL、检查点恢复与全量执行对比。
- `samples/`：已生成的 `cases.jsonl`、`write_batches.jsonl`、`sample_metadata.json`。
- `reports/`：SpMM、SpMSpM、组件检查、恢复演示与汇总 JSON 报告。
- `tests/`：38 个单元/集成测试。
- `docs/DESIGN.md`、`docs/VALIDATION.md`：设计说明与实际验证记录。

## 环境

已使用隔离环境验证：

```text
Python 3.11.18
NumPy 2.4.6
SciPy 1.17.1
Linux / WSL2 x86_64，无 GPU
```

同一套测试也在 Python 3.10.12、NumPy 1.26.4、SciPy 1.13.1 下通过。`pyproject.toml` 声明兼容 Python `>=3.10`。

安装：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 快速开始

```bash
# 确定性生成样本
.venv/bin/python -m sparse_inc.cli generate --out samples --seed 20261123

# 运行 SpMM 案例并输出完整 JSON 报告
.venv/bin/python -m sparse_inc.cli run-case \
  --manifest samples/cases.jsonl \
  --case-id spmm_inc_demo \
  --report reports/spmm_case_report.json

# 运行 SpMSpM 案例
.venv/bin/python -m sparse_inc.cli run-case \
  --manifest samples/cases.jsonl \
  --case-id spspmm_inc_demo \
  --report reports/spspmm_case_report.json

# 一次执行全部验收演示，包含拒绝写入、快照、资源、并发与 SIGKILL 恢复
.venv/bin/python -m sparse_inc.cli demo --samples samples --reports reports --runs runs

# 独立执行 SIGKILL/恢复演示
.venv/bin/python scripts/recovery_demo.py \
  --manifest samples/cases.jsonl --reports reports --runs runs
```

`demo` 成功时会写出 `reports/demo_report.json`，退出码为 `0`。

## 样本格式

`samples/cases.jsonl` 每行一个案例，核心字段：

```json
{
  "case_id": "spmm_inc_demo",
  "operation": "spmm",
  "shape_mnk": [20, 7, 12],
  "nnz_a": 54,
  "nnz_b": 23,
  "dtype": "float64",
  "seed": 20261123,
  "base_csr": {"csr_a": {"indptr": [], "indices": [], "values": []}, "csr_b": {}},
  "write_batches": []
}
```

- `shape_mnk = [M, N, K]`：`A` 为 `M x K`，`B` 为 `K x N`，结果为 `M x N`。
- `base_csr.csr_a`、`base_csr.csr_b` 使用标准 CSR 三段数组。
- SpMM 将 `B` 视为稠密右操作数；SpMSpM 保留 `B` 的稀疏结构。两种模式都以 float64 稠密矩阵乘为真值。
- 每个写入批次覆盖半开行区间 `[row_start, row_end)`，`mode` 为 `replace` 或 `invalidate`。
- `replace` 必须为区间内每一行提供唯一的列索引和值；`invalidate` 将该区间所有行置为空行。

批次校验和为规范化 JSON 的 SHA-256，覆盖：

```text
batch_id, write_version, row_start, row_end, mode, rows
```

列索引与行对象在计算校验和前会规范化排序；校验和格式为 `sha256:<hex>`。

## 写入规则

- `write_version` 必须严格大于当前已接受账本版本；否则 `E_WRITE_STALE`。
- 内容校验和不符返回 `E_WRITE_CORRUPT`。
- 行区间越界、缺行或列索引越界返回 `E_WRITE_RANGE`。
- 行内列索引重复、重复 `batch_id`、非法模式返回 `E_WRITE_CONFLICT`。
- 多个批次覆盖同一行时，按“版本严格递增 + 后接受者覆盖”规则裁决。
- 相同内容以更高版本重复写入会被接受，账本版本推进，但结果块摘要保持逐字节一致。
- 所有被拒绝批次在锁外完成内容校验，在提交前失败，不改变账本、行状态或结果快照。

## API 示例

```python
from sparse_inc import ComponentConfig, SparseIncrementalService, multiply, spmm, spspmm
from sparse_inc.case import load_cases

cases = load_cases("samples/cases.jsonl")
case = cases["spmm_inc_demo"]

# 独立一次性算子
dense_c = spmm(case.a, case.b)
sparse_c = spspmm(case.a, case.b)
unified_c = multiply(case.a, case.b, operation="spmm")

# 增量统一入口
service = SparseIncrementalService(
    case,
    ComponentConfig(row_block_size=4, max_parallel_intervals=4),
    checkpoint_dir="runs/example",
)
for index, batch in enumerate(case.write_batches):
    result = service.submit_batch(batch, batch_index=index)
    assert result["business_code"] == "OK"

check = service.self_check()
state = service.export_bundle()
dense_result = service.materialize()
```

`submit_batch` 在单个原子提交中完成“校验 → 行状态覆盖/失效 → 受影响块重算 → 账本与结果快照整体替换 → 检查点落盘”。未受影响块保留原 `RowBlock` 对象，不重新乘法。

## 业务码

| 业务码 | 含义 | CLI 退出码 |
| --- | --- | ---: |
| `OK` | 成功 | 0 |
| `E_SHAPE_INVALID` | 形状非法 | 10 |
| `E_DTYPE_UNSUPPORTED` | dtype 非 float64 | 11 |
| `E_CSR_STRUCT` | 行指针/列索引/CSR 结构非法 | 12 |
| `E_NNZ_MISMATCH` | 声明 nnz 与存储不符 | 13 |
| `E_WRITE_STALE` | 写入版本回退或未递增 | 20 |
| `E_WRITE_CORRUPT` | 批次内容校验和不符 | 21 |
| `E_WRITE_RANGE` | 行区间/列索引越界或行不完整 | 22 |
| `E_WRITE_CONFLICT` | 列重复、批次 ID 重复、模式非法 | 23 |
| `E_WRITE_BUSY` | 已有写入持有提交锁 | 24 |
| `E_RESOURCE_LIMIT` | 严格资源策略超限 | 25 |
| `E_INCONSISTENT` | 增量一致性自检失败 | 30 |
| `E_SNAPSHOT_ROLLBACK` | 快照回退或同版本异校验和 | 40 |
| `E_SNAPSHOT_CORRUPT` | 快照/检查点损坏 | 41 |
| `E_RECOVERY_FAILED` | 恢复请求无可用检查点 | 42 |
| `E_INVALID_REQUEST` | 参数或清单结构非法 | 43 |
| `E_CASE_NOT_FOUND` | 清单中找不到指定案例 | 44 |

错误 JSON 包含 `business_code`、`case_id`、`segment`、`message` 与结构化细节。

## 并发、资源与恢复

- 写入提交使用非阻塞独占锁；竞争写入立即得到 `E_WRITE_BUSY`。
- 查询先同步取得不可变状态视图，再在锁外物化；不会看到半个批次或新旧混合块。
- `max_resident_batches` 限制并发驻留提交；超限返回 `E_RESOURCE_LIMIT`。
- `max_row_buffers` 与 `max_parallel_intervals` 限制重算缓冲与并行块数。`resource_policy="strict"` 返回 `E_RESOURCE_LIMIT`；`"degrade"` 分块执行并标记 `row_buffer_limit_chunked` / `parallel_interval_limit_capped`。
- 每次成功提交都以“临时文件 + fsync + 原子 replace”写入完整检查点。
- 恢复只解码、校验账本与结果块，不重算已提交行区间；未提交的 `.pending` 中间文件被丢弃。
- 强制终止演示使用真实 `SIGKILL`，因此不生成退出报告；恢复后的检查点、账本、结果和快照与不间断执行逐字节一致。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

当前结果：`38/38` 通过。完整演示：`26/26` 检查通过。

实际指标：

- SpMM 与 float64 稠密参考最大绝对误差：`8.881784197001252e-16`。
- SpMSpM 最大绝对误差：`4.440892098500626e-16`。
- 版本回退与校验和损坏写入均被拒绝，账本与结果快照不变。
- SIGKILL 后恢复检查点与全量执行检查点逐字节相等。

详细设计与验证见：

- `docs/DESIGN.md`
- `docs/VALIDATION.md`
- `reports/demo_report.json`
- `reports/component_checks.json`
- `reports/recovery_demo.json`
