# sparse-recall

本地可集成的稀疏召回索引构建与查询组件。项目从零实现文档/查询双批次流水线：

`批次摄入与词项校验 -> 倒排累积与分区统计 -> 固定维度稀疏特征投影 -> 查询打分与 top-k`

组件只依赖 Python 标准库与 NumPy，不访问网络，不使用容器或外部服务；本地进程池固定使用
`spawn` 启动方式，适用于 Windows 本地进程。

## 快速开始

推荐 Python 3.11 或更高版本：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install numpy==1.26.4
.venv\Scripts\python -m pip install -e .
```

Linux/macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install numpy==1.26.4
python -m pip install -e .
```

生成固定种子样本并执行完整示例：

```bash
python -m sparse_recall generate --out data/demo --seed 20261124
python -m sparse_recall run \
  --data-dir data/demo \
  --workdir artifacts/demo \
  --fresh \
  --slots 2 \
  --workers 2 \
  --fma-budget 26000 \
  --memory-budget 20000 \
  --compare-mode tolerance \
  --tolerance 1e-9
python -m sparse_recall verify --workdir artifacts/demo
```

一条命令完成生成、运行和报告：

```bash
python -m sparse_recall demo --root artifacts/demo --fresh
```

也可安装后直接使用 `sparse-recall` 命令。`scripts/run_demo.py` 提供 Windows spawn 安全的脚本入口。

Python API：

```python
from sparse_recall.ingest import ingest_dataset
from sparse_recall.pipeline import PipelineConfig, run_pipeline
from sparse_recall.scheduler import SchedulerConfig
from sparse_recall.weights import load_weight_table


def main() -> None:
    weights = load_weight_table("data/demo/term_weights.json")
    ingested = ingest_dataset(
        "data/demo/documents.jsonl",
        "data/demo/queries.jsonl",
        weights,
    )
    scheduler = SchedulerConfig(
        slots=2,
        fma_budget=26000,
        memory_budget=20000,
        worker_shards=2,
        chunk_size=8,
        query_candidate_docs=len(ingested.documents),
        avg_doc_nnz=18,
    )
    result = run_pipeline(
        document_path="data/demo/documents.jsonl",
        query_path="data/demo/queries.jsonl",
        weights_path="data/demo/term_weights.json",
        workdir="artifacts/api-run",
        scheduler_config=scheduler,
        config=PipelineConfig(workers=2, use_process_pool=True),
    )
    print(result.report["status"])


if __name__ == "__main__":
    main()
```

## 目录

```text
sparse_recall/
  models.py        输入模型与词项校验
  ingest.py        两个 JSONL 流的确定性摄入
  weights.py       NumPy 固定种子稀疏词项权重表
  inverted.py      分块倒排累积、Neumaier 补偿求和、全局归约
  projection.py    固定维度稀疏投影与稀疏点积
  scheduler.py     槽位/FMA/内存三重预算准入计划
  workers.py       spawn worker 入口与结构化异常封装
  executor.py      本地进程池、确定性分块和归并
  scoring.py       门限比较、次正规数保护、稳定 top-k
  snapshots.py     原子、校验和、非阻塞版本化快照
  consistency.py   跨阶段独立重算自检
  pipeline.py      四阶段流水线和恢复
  cli.py           命令行入口
tests/             20 项验收测试
data/demo/         固定种子样本
artifacts/demo/    实际演示运行、计划、快照和报告
REPORT.md          实际验证结果
```

## 输入格式

`documents.jsonl`：

```json
{"batch_no":1,"doc_id":"doc-00000","doc_len":12,"terms":{"t001":3,"t007":4,"t019":5}}
```

`queries.jsonl`：

```json
{"batch_no":1001,"priority":3,"query_id":"qry-00000","terms":{"t001":2,"t019":1},"top_k":5}
```

- 文档字段为 `doc_id`、`batch_no`、`terms`、`doc_len`。
- 查询字段为 `query_id`、`batch_no`、`terms`、`top_k`；可选 `priority` 参与准入排序。
- `terms` 推荐使用 `{term: frequency}`；也接受词项字符串列表或 `[term, frequency]` 对。
- 文档 `doc_len` 必须等于词频之和；未知词项、非正词频、重复 ID 均返回结构化校验错误。
- 摄入始终按 `(batch_no, doc_id/query_id, 输入行号)` 排序，不依赖 JSONL 物理顺序。

`term_weights.json` 由固定种子 NumPy 生成。默认词表 `t000`…`t047`，维度 64，每词 3 个非零维度。

## 调度规则

每个批次的估算口径显式写在计划中：

- 文档 FMA：`sum(每文档唯一词项数) * nnz_per_term`
- 文档内存：`2048 + unique_terms*(32 + nnz*16) + docs*128 + term_entries*16`
- 查询 FMA：查询词项投影 + `queries * candidate_docs * avg_doc_nnz` + 有效 top-k 汇总
- 查询内存：词项缓存 + 每查询固定开销 + top-k 输出预留

全局裁决顺序：

1. 查询优先级降序
2. 词项去重收益降序
3. 批次规模升序
4. 文档批次先于查询批次
5. `batch_no` 升序

执行顺序和并发波次由同一优先级列表确定性生成。每个波次同时受槽位数与驻留内存约束；整份计划受
总 FMA 预算约束。预算不足时依次尝试减少并行分片、文档执行前缀、查询执行前缀和有效 `top_k`。

计划中的每个批次包含：

- `estimate`：完整代价、词项数、分片需求
- `priority`：查询优先级、去重收益、批次规模
- `weight_reuse`：共享词项数与共享权重复用字节
- `status`：`admitted`、`degraded` 或 `rejected`
- `code`：`E_COMPUTE_BUDGET`、`E_MEMORY_BUDGET` 或 `E_SLOT_LIMIT`
- `reason_codes` / `reasons`：完整裁决依据
- `execution`：实际文档/查询数、实际 `top_k`、分片数、FMA、内存和波次

## 确定性约定

- 所有 JSON 使用排序键、紧凑分隔符和 UTF-8 编码。
- 浮点结果规范到 12 位小数（可配置），`-0.0` 归一为 `0.0`，绝对值小于
  `1e-300` 的次正规噪声折叠为 `0.0`。
- 统计归约顺序固定为 `batch_no, chunk_index, doc_id, term`。
- 稀疏向量维度按升序存储；点积使用 `math.fsum` 融合。
- 分数相同时按 `doc_id` 升序。
- `tolerance` 模式使用容差桶排序，避免门限附近的微小舍入差异翻转 top-k。
- 严格模式要求 `score > threshold`；容差模式要求 `score >= threshold - tolerance`。

## 检查点与恢复

四个阶段均在批次边界提交检查点：

- `ingest`
- `inverted`
- `projection`
- `scoring`

快照字段包括 `version`、`stage`、`completed_batches`、`failed_batches`、完整 `payload` 和
`content_sha256`。批次键使用 `doc:<batch_no>` / `query:<batch_no>`，避免两个流的批次号冲突。
写入过程为同目录临时文件、`fsync`、`os.replace`，随后同步目录；不会对旧快照做增量修补。

恢复规则：

- 只读取单个最新已提交快照。
- 已完成或已终止失败的批次不重算。
- 未提交的统计/投影缓冲不进入快照；重启后从输入和已提交状态重建。
- 版本回退或同版本不同内容返回 `E_SNAPSHOT_ROLLBACK`。
- JSON/校验和损坏返回 `E_SNAPSHOT_CORRUPT`。
- 提交锁使用线程锁加 OS 非阻塞文件锁；冲突立即返回 `E_STAGE_BUSY`，不等待。

强制退出演示：

```bash
# 第 17 次批次边界提交后直接退出，退出码 137
python -m sparse_recall run --data-dir data/demo --workdir artifacts/recovery-interrupted \
  --fresh --fma-budget 26000 --memory-budget 20000 --crash-after-commits 17

# 从版本 17 继续到版本 22
python -m sparse_recall run --data-dir data/demo --workdir artifacts/recovery-interrupted \
  --fma-budget 26000 --memory-budget 20000
```

## 输出

`artifacts/demo/outputs/`：

- `plan.json`：完整调度计划
- `validation.jsonl`：每条输入记录的词项校验结果
- `vectors.jsonl`：已执行文档的固定维度稀疏向量
- `statistics.json`：分区、全局统计和归约口径
- `inverted_index.json`：分区统计与完整倒排 postings
- `results.jsonl`：每个已执行查询的稳定 top-k
- `failures.jsonl`：worker/阶段结构化失败
- `consistency.json`：跨阶段自检

工作目录还包含 `checkpoints/checkpoint.json`、`checkpoints/statistics.json`、
`recovery_report.json` 和 `run_report.json`。

## 自检

流水线结束前独立执行：

- 四阶段计划集合与 `completed ∪ failed` 集合一致
- 本次重算集合与快照复用集合无交集，并覆盖全部受影响批次
- 每个分区统计由逐文档明细单块重算
- postings 数量与词项 `df` 一致
- 全局统计由分区按声明顺序重算
- 每个已提交文档向量重新投影
- 每个查询的 top-k 从稀疏查询/文档向量独立重算

任何不一致返回 `E_INCONSISTENT`。

## 测试

```bash
python -m pytest -q
# 或
PYTHONPATH=. python -m unittest discover -s tests -v
```

测试覆盖确定性样本、摄入校验、补偿求和、逐文档统计重算、预算三类错误码、spawn 异常隔离、
快照损坏/回退/忙碌、投影中途强制退出恢复、单批次失败隔离和 top-k 稳定性。

实际验证结果见 [REPORT.md](REPORT.md)。
