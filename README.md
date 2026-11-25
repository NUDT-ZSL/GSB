# RAG / 工具调用链本地诊断工作台

这是一个从零实现的确定性本地工作台，把检索增强（RAG）与工具调用链建模为有向无环计算图，在并发、逻辑时间和驻留内存三重预算下完成：

- v1 / v2 / v3 三种工具结果格式读取与校验；
- 倒排、前置词项扩展、固定种子向量 LSH 三路证据召回；
- 按 `tool_calls` 顺序组装可独立重算的答案片段；
- 本地进程池并行调度、确定性分块与三预算准入；
- 质量 / 内存 / 延迟冲突裁决；
- float64 分块与全量结果逐字节一致性检查；
- 内容寻址缓存、依赖失效传播、原子版本化快照；
- JSON 诊断报告与独立 HTML 可视化报告。

实现仅使用 Python 标准库和 NumPy，不使用 Docker 或其他容器环境。

## 环境

实际验证环境：

```text
Linux WSL2 / Ubuntu 22.04.5 LTS
Python 3.12.13
NumPy 2.5.5
```

推荐使用 Python 3.11 或更高版本：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python --version
.venv/bin/python -c "import numpy; print(numpy.__version__)"
```

如果本机已有 `uv`，也可执行：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 快速开始

### 1. 生成固定种子样本

```bash
.venv/bin/python generate_samples.py --out data
```

生成文件：

| 文件 | 用途 |
| --- | --- |
| `data/requests.jsonl` | 9 条多轮请求，共 48 次工具调用 |
| `data/tool_results.json` | 快照版本 7，v1/v2/v3 工具结果与校验和 |
| `data/schedule.json` | 并发、逻辑时间、内存、精度与仲裁配置 |
| `data/evidence_corpus.jsonl` | 固定种子的检索证据语料 |
| `data/tool_results.next.json` | 快照版本 8；`res/weather/002` 升到条目版本 2 |
| `data/schedule.tuned.json` | 仅调整内存预算的调度配置对照样本 |
| `data/manifest.json` | 输入文件 SHA-256 与预期业务码 |

证据语料同时嵌入 `tool_results.json` 顶层的 `evidence_corpus` 字段，独立
`evidence_corpus.jsonl` 仅用于直接检查。若只提供题目要求的请求清单、工具
结果仓库和调度配置三份输入，运行器会自动使用内嵌语料。

请求记录除题目要求字段外，样本生成器还写入了可选的 `required_lanes` 和 `seed_offset`。`required_lanes` 是显式并发需求，缺省为 1；它使“单请求并发需求超过硬上限”可以稳定返回 `E_CONCURRENCY_BUDGET`。

### 2. 运行诊断

```bash
.venv/bin/python -m ragdiag run \
  --repository data/tool_results.json \
  --requests data/requests.jsonl \
  --schedule data/schedule.json \
  --corpus data/evidence_corpus.jsonl \
  --cache .workbench-cache \
  --out-json reports/diagnostic.json \
  --out-html reports/diagnostic.html
```

### 3. 运行版本升级失效传播

```bash
.venv/bin/python -m ragdiag invalidate \
  --repository data/tool_results.next.json \
  --baseline-repository data/tool_results.json \
  --requests data/requests.jsonl \
  --schedule data/schedule.json \
  --corpus data/evidence_corpus.jsonl \
  --cache .workbench-cache \
  --out-json reports/invalidation.json \
  --out-html reports/invalidation.html
```

### 4. 测试与一键验收

```bash
.venv/bin/python -m unittest discover -s tests -v
bash scripts/run_acceptance.sh
```

独立校验已有报告：

```bash
.venv/bin/python -m ragdiag verify \
  --report reports/diagnostic.json
```

`run_acceptance.sh` 会使用临时缓存执行基线、重复运行、版本升级失效与测试，并输出：

- `reports/acceptance-baseline.html`
- `reports/acceptance-invalidation.html`
- `reports/acceptance-summary.json`

## 架构

```text
request source ───────────────┐
                              ├──> merge ──> snapshot / report
tool result read ─────────────┤
                              │
evidence index ──> 3-route retrieve
                              │
previous turn merge ──────────┘  (multi-turn DAG edge)
```

模块职责：

| 模块 | 职责 |
| --- | --- |
| `ragdiag/formats.py` | v1/v2/v3 解析、版本分派、字段/数值/校验和检查 |
| `ragdiag/retrieval.py` | 固定种子索引、三路召回、加权贡献与稳定排序 |
| `ragdiag/merge.py` | 按调用计划合并片段、答案摘要、独立重算 |
| `ragdiag/planning.py` | DAG 拓扑、节点键及上游依赖键 |
| `ragdiag/arbitration.py` | 质量/内存/延迟 profile 投影与优先级裁决 |
| `ragdiag/scheduler.py` | 三预算准入、确定性分块、进程池 worker |
| `ragdiag/cache.py` | 内容寻址对象、快照落盘、原子 active 指针替换 |
| `ragdiag/invalidation.py` | 新旧 DAG 节点键差分、失效阶段与覆盖检查 |
| `ragdiag/runtime.py` | 端到端执行、全量重算、块一致性、快照激活 |
| `ragdiag/report.py` | 独立一致性校验、JSON/HTML 报告 |

## 核心设计

### 三格式读取

- v1：`v1|col1|col2|...`，通过条目 `columns` 固定字段顺序和字段数；无 `columns` 时兼容 `key=value` 列。
- v2：逐行 `key=value`，首行必须为 `format_version=v2`，按 `fields` 检查键，按 `numeric_fields` 检查数值。
- v3：JSON 对象或嵌套对象，按 `required_paths` 与 `numeric_paths` 校验。
- 条目携带 `version`、`format_version`、`checksum`；读取前验证原始内容校验和。
- 未知版本返回 `E_VERSION_UNSUPPORTED`；字段数不符、键缺失、数值不可解析、校验和错误返回 `E_FORMAT_CORRUPT`。
- `read_repository_bytes()` 使用规范 JSON 序列化，同输入同配置逐字节一致。

### 三路检索与打分

1. **倒排召回**：词项 IDF 与词频饱和打分；
2. **前置扩展**：对查询词匹配固定数量词表前缀；
3. **向量近似**：固定 seed 哈希嵌入 + 超平面 LSH 分桶，按桶汉明距离探查。

每个候选包含 `route`、`raw_score`、`weight`、`weighted` 明细。最终分数为各加权贡献的 `math.fsum`，按 `(-score, doc_id)` 排序，确保同分时片段标识升序。

### 图节点键

节点键统一为：

```text
sha256(canonical({
  "stage": <stage>,
  "upstream": <上游节点键或有序列表>,
  "data": <原始内容/查询/请求摘要>,
  "config": <该阶段实际消费的配置指纹>
}))
```

- 工具读取键包含原始内容哈希、格式版本、条目版本、请求版本和读取器配置；
- 检索键包含语料索引哈希、查询、前序轮次合并键和检索配置；
- 合并键包含有序读取键、检索键、完整调度配置与合并/精度配置。

因此缓存键同时覆盖内容、版本、调度配置和上游产物哈希，同时避免无关配置变化导致全图失效。

### 三预算准入

调度按 `(request_id, turn_no)` 稳定排序并按轮累计：

- `required_lanes > concurrency_limit`：`E_CONCURRENCY_BUDGET`
- 单请求或轮累计逻辑耗时超限：`E_TIME_BUDGET`
- 新增驻留工具结果字节超过缓存上限：`E_MEMORY_BUDGET`

若同时违反多个预算，按配置 `budget_precedence` 选择主业务码，其余违规项保留在 `violations` 中。通过准入的请求按 `precision.block_size` 切成稳定块，再以本地 `ProcessPoolExecutor` 执行检索和合并；worker 结果按块顺序、块内请求顺序归并。

### 冲突裁决

`arbitration.profiles` 同时投影候选宽度、证据扩展和并发度。每个 profile 计算：

- 质量：候选覆盖、证据扩展、工具调用扇出与已准入覆盖率；
- 内存：并发块内工具结果与候选驻留峰值；
- 延迟：块 makespan；

然后按 `priority_order` 做字典序选择。报告保留全部 profile 矩阵、选中依据、对照 profile、受影响请求和预算硬约束原因。

### 数值稳定性

- 所有得分最终进入 `np.float64` C 连续数组；
- 支持 `half_even`、`half_up`、`toward_zero`、`up`；
- 可配置次正规数归零保护；
- 分块结果与全量结果先比较 float64 字节，再比较规范 JSON 字节；
- 数值不一致抛出 `E_BLOCK_MISMATCH`，并给出首个偏差的 `byte_index`、`element_index` 和左右值。

### 缓存、失效与快照

缓存对象位于 `<cache>/objects/<key[:2]>/<key>.json`，内容不可变并以原子写入落盘。快照 manifest 记录：

- 仓库快照版本、条目指纹；
- 生效调度哈希与选中 profile；
- 全图节点键；
- 请求业务码与答案摘要；
- 块一致性摘要。

快照先完整写入 `snapshots/`，再通过同目录 `os.replace` 原子替换 `active_snapshot.json`。旧快照保留，可通过 `activate` 回滚。

再次运行同一缓存且未显式传 baseline 时，工作台会自动把当前计划与 active
快照的节点键比较，输出自动失效清单；重复相同输入时 `recompute_nodes=0`。

```bash
.venv/bin/python -m ragdiag snapshots --cache .workbench-cache
.venv/bin/python -m ragdiag activate <snapshot_id> --cache .workbench-cache
```

## 实际验收结果

以下为本机固定种子样本的实际运行结果。

基线运行：

```json
{
  "business_code": "E_OK",
  "business_codes": {
    "E_CONCURRENCY_BUDGET": 1,
    "E_MEMORY_BUDGET": 1,
    "E_OK": 6,
    "E_TIME_BUDGET": 1
  },
  "requests_total": 9,
  "requests_ok": 6,
  "block_equivalence": "identical",
  "selected_profile": "expanded",
  "repository_snapshot_version": 7
}
```

重复运行同一输入：

```text
6 个成功请求全部缓存命中；3 个被预算拒绝的请求仍返回原业务码。
```

工具结果 `res/weather/002` 从条目版本 1 升至 2、仓库快照从 7 升至 8：

```json
{
  "affected_requests": [
    "conv-001@1",
    "conv-001@2",
    "conv-005@1",
    "conv-007@1"
  ],
  "affected_stages": ["merge", "retrieve", "tool_read"],
  "recompute_nodes": 38,
  "reuse_nodes": 23,
  "coverage": {
    "affected_requests_covered": true,
    "disjoint": true,
    "missing_affected_requests": []
  }
}
```

仅修改调度内存预算：

```json
{
  "affected_requests": [
    "conv-001@1",
    "conv-001@2",
    "conv-002@1",
    "conv-003@1",
    "conv-004@1",
    "conv-005@1",
    "conv-006@1",
    "conv-007@1",
    "conv-008@1"
  ],
  "affected_stages": ["merge", "retrieve"],
  "recompute_nodes": 10,
  "reuse_nodes": 53
}
```

说明：所有合并阶段因调度配置指纹变化而失效；读取节点全部复用；只有 `conv-001@2` 的检索节点因前序合并键变化而级联失效。

测试结果：

```text
Ran 22 tests
OK
```

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_OK` | 请求成功或整次诊断完成 |
| `E_VERSION_UNSUPPORTED` | 工具结果版本未知或与调用要求不一致 |
| `E_FORMAT_CORRUPT` | 字段数、键、数值、版本头或校验和损坏 |
| `E_CONCURRENCY_BUDGET` | 并发需求超过硬上限 |
| `E_TIME_BUDGET` | 单请求或轮累计逻辑时间超过预算 |
| `E_MEMORY_BUDGET` | 驻留工具结果缓存超过内存上限 |
| `E_BLOCK_MISMATCH` | 分块与全量结果不一致 |
| `E_INCONSISTENT` | 缓存、报告、失效集合或跨模块自检不一致 |

## 目录

```text
ragdiag/                  核心实现
generate_samples.py       样本生成入口
scripts/generate_samples.py
scripts/run_acceptance.sh
tests/                    22 个 unittest 用例
data/                     固定种子生成输入
reports/                  实际诊断报告
```

