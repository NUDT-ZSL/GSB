# CQIR：对话式检索与引用互操作本地应用

本项目从空白工作区构建，提供一条可重复、无外部服务的对话式检索与生成式答案流水线，重点验证：

- 答案引用 v1、v2、v3 三种书写形式的解析、迁移与跨格式裁决。
- 文档版本提升或删除后的答案片段增量失效、引用重放与逐字节可复现。
- 文档版本表、引用快照和答案快照的整体替换、回退保护、校验和检查与并发写控制。
- 本地交互式控制台、固定种子样本、进程池批处理和自动化验收测试。

应用运行时不访问网络、不使用 GPU、不依赖 Docker，答案由本地抽取式片段确定性合成。

## 环境确认

实际执行环境：

- 宿主为 Windows，当前命令运行在 WSL2 Ubuntu 22.04 中，内核标识为 `microsoft-standard-WSL2`。
- Python `3.10.12`。
- NumPy `1.26.4`。
- 当前不是原生 Windows `cmd`/PowerShell 进程，因此不能把本次结果表述为“已在原生 Windows 验证”；Windows 运行步骤见下方。

任务要求的 Windows 安装路径：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m cqir verify
python -m cqir serve --host 127.0.0.1 --port 8765
```

WSL/Linux 路径：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m cqir verify
python -m cqir serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765`，页面会自动运行一次完整演示。

## 快速演示

生成固定种子样本：

```bash
python -m cqir generate --root .
```

运行完整验证并写出产物：

```bash
python -m cqir verify
```

指定会话和轮次：

```bash
python -m cqir demo --conv-id conv-001 --turn-no 2
```

启动本地控制台：

```bash
python -m cqir serve --port 8765
```

控制台支持：

- 选择本地多轮查询并重新执行演示。
- 在 v1、v2、v3 格式视图之间切换和复制原文。
- 查看三路召回路名与原始分、片段分数明细、引用装配、迁移补全/丢弃记录。
- 查看增量重算集合、复用集合、受影响引用、快照版本和预期业务错误码。
- 查看跨模块一致性自检状态。

## 目录

```text
cqir/
  generation.py    固定种子样本生成
  text.py          Unicode 归一化、多轮上下文合并
  retrieval.py     倒排、类别热门、固定种子向量近似与归并
  snippets.py      码点 span 片段抽取与分数明细
  pipeline.py      确定性流水线、内容寻址缓存、增量失效
  formats.py       v1/v2/v3 序列化、分派解析与错误定位
  migration.py     六向迁移、补全来源、丢弃明细、span 审计
  arbitration.py   跨格式语义裁决
  snapshot.py      版本化快照、校验和、非阻塞并发锁
  selfcheck.py     跨模块一致性自检
  parallel.py      本地进程池批处理
  demo.py          可重复端到端演示
  server.py        本地 HTTP 服务和 API
  web/index.html   单页交互控制台
tests/test_cqir.py 30 项验收测试
samples/           固定种子查询、文档与版本提升集合
artifacts/         演示结果、验证摘要和三种格式答案文件
```

## 样本

`samples/queries.jsonl` 包含 9 个多轮查询，每行字段为：

```json
{"conv_id":"conv-002","locale":"zh-CN","query_text":"incremental invalidation 如何复用 snippet 与 recall？","session_hint":"topic=incremental;channel=api","turn_no":1}
```

`samples/documents.json` 包含 7 个文档，每个文档保留业务要求的 `doc_id`、`terms`、`doc_len`、`doc_version`，并补充 `category`、`popularity`、`paragraphs` 以支持类别召回、段落角色和码点片段：

```json
{
  "doc_id": "doc-002",
  "category": "incremental",
  "popularity": 0.629413,
  "terms": {"citation": 1, "incremental": 2},
  "doc_len": 97,
  "doc_version": 1,
  "paragraphs": [{"role": "definition", "text": "..."}]
}
```

版本提升集合 `samples/documents_bumped.json` 将 `doc-002` 从 v1 提升到 v2，新增 `publish`、移除 `legacy`，并删除 `doc-005`。变更说明位于 `samples/change_set.json`。

## 流水线

1. 查询归一化：Unicode NFKC、casefold、稳定分词与停止词过滤。
2. 多轮上下文合并：同一 `conv_id` 当前轮及前序轮按固定衰减权重合并，所有 token 显式排序。
3. 三路召回：
   - `inverted`：查询词项权重与文档词频的确定性打分。
   - `category_popular`：`session_hint` 的 `topic` 类别匹配与固定文档热门度。
   - `vector_approx`：NumPy 固定种子特征散列随机投影近似向量。
4. 候选按 `doc_id` 归并，保留每路 `route` 和 `raw_score`；综合分同分时按 `doc_id` 升序。
5. 片段选择：仅从召回文档抽取码点 span，按 `confidence` 与 `coverage` 加权给分：

   ```text
   final_score = q6(0.65 * confidence + 0.35 * coverage)
   ```

6. 引用装配：每条引用对应一个唯一片段，附带置信区间、段落角色、引用顺序权重、修订号和多轮上下文引用链。
7. 答案合成：按引用顺序拼接抽取片段，生成稳定 UTF-8 文本。

每个阶段通过规范化 JSON 和 `sha256` 内容寻址缓存。流水线不读取系统时间，不以字典遍历顺序作为业务顺序。

## 引用格式

### v1 扁平行式

```text
CQIR-ANSWER/1
conv_id="conv-002"
turn_no=1
answer_text="本地抽取式回答：..."
answer_sha256=<答案文本 SHA-256>
doc_version=doc-002:1
doc-002|0|72|0.913462
```

引用行固定为 `doc_id|start|end|score`，没有置信区间、段落角色、上下文链或修订号。

### v2 扩展列

```text
CQIR-ANSWER/2
conv_id="conv-002"
turn_no=1
answer_text="本地抽取式回答：..."
answer_sha256=<答案文本 SHA-256>
doc_version=doc-002:1
doc-002|0|72|0.913462|0.833462|0.993462|definition|1.000000
```

引用行字段为：

```text
doc_id|start|end|score|confidence_low|confidence_high|paragraph_role|order_weight
```

### v3 结构化 JSON

规范 JSON 包含：

- `format: "CQIR-ANSWER/3"`
- `conv_id`、`turn_no`、`answer_text`、`answer_sha256`
- `doc_versions`
- `context_chain`
- `snippets`：片段文本、码点 span 与完整分数明细
- `citations`：置信区间、段落角色、顺序权重、`revision`、逐引用 `context_refs`

解析器先看格式头；没有格式头时依据竖线字段数或 JSON 字段集分派。未知版本返回 `E_FORMAT_VERSION_UNSUPPORTED`。截断头、列数不符、必填字段缺失、数值不可解析、答案校验和不符返回 `E_FORMAT_CORRUPT`，`details` 包含 `field`、`line`、`column` 和必要的期望/实际值。

## 迁移与裁决

六条有向迁移路径均已实现：

- v1 → v2：补全置信区间、段落角色和顺序权重，逐项记录补全来源。
- v1/v2 → v3：补全多轮上下文链与 `revision=1`，逐项记录来源。
- v3 → v2：丢弃 `context_chain`、`context_refs`、`revision` 并列出明细。
- v2/v3 → v1：丢弃 v1 无法表达的置信区间、角色、顺序权重、上下文链与修订号。

迁移完成后会重新序列化、重新解析并比较源/目标引用 span。任何偏移改写或截断返回 `E_SPAN_DRIFT`。

三格式裁决会把 v1、v2、v3 都迁移到 v3 规范语义，比较：

- 答案文本与文档版本。
- 片段集合和顺序。
- 引用集合、排序和全部引用字段。
- 多轮上下文引用链与逐引用修订号。

若 v1 缺失字段被默认补全后仍与 v2/v3 不同，返回 `E_SEMANTIC_INCOMPLETE`，列出具体不可还原字段，不会静默宣称等价。

## 增量失效

文档版本提升或删除时：

1. 使用文档内容摘要使受影响文档的逐路召回和片段缓存键失效。
2. 未变化文档继续复用每路原始分和片段候选。
3. 重算召回归并、片段选择、引用装配和答案合成等受影响后缀。
4. 输出 `recompute_set`、`reuse_set`、受影响引用、未受影响引用、删除文档和变更文档。
5. 与使用新文档集合从零重算的结果比较规范化答案字节和 v3 字节。

演示中的基线任务使用 `conv-002:1`；`doc-002` 升级到 v2、`doc-005` 被删除。增量结果与从零重放的 v3 字节完全一致。

## 快照与并发

`VersionedSnapshotStore` 为每个快照保存：

```json
{
  "schema": "cqir-snapshot/1",
  "namespace": "citations_snapshot",
  "cache_key": "session",
  "version": 2,
  "payload": {},
  "checksum": "<sha256(version + payload)>"
}
```

规则：

- 每次写入都是完整 envelope 的原子替换，不做增量修补。
- 版本必须单调递增，回退或等于当前版本返回 `E_SNAPSHOT_ROLLBACK`。
- 乐观并发传入的 `expected_version` 或 `expected_checksum` 已变化时返回 `E_SNAPSHOT_ROLLBACK`。
- 内容损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`。
- 同一 `namespace/cache_key` 使用 `O_CREAT|O_EXCL` 非阻塞独占锁；冲突方立即得到 `E_STAGE_BUSY`。

演示会分别维护 `doc_version_table`、`citations_snapshot`、`answer_snapshot`，并展示版本 1 → 版本 2 的整体替换。

## 一致性自检

自检覆盖：

- 各阶段 `conv_id` 与答案 `conv_id` 一致。
- 召回、片段选择、装配阶段 `doc_id` 集合可核对。
- 每条引用恰出现一次。
- 片段集合与引用集合一一对应。
- 增量保留集与剔除集无交集。
- 快照版本号存在且可核对。

任何不一致返回 `E_INCONSISTENT`，`details.categories` 给出不一致类别，`details.snapshot_versions` 和会话轮次给出上下文。

## 业务错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_SPAN_OUT_OF_RANGE` | 引用或片段 span 为空、为负或超出文档码点长度 |
| `E_CITE_DUP` | 同一 `doc_id + start + end` 重复引用 |
| `E_FORMAT_VERSION_UNSUPPORTED` | 未支持的格式头版本或目标版本 |
| `E_FORMAT_CORRUPT` | 截断头、列数不符、必填字段缺失、数值不可解析、校验和不符 |
| `E_SPAN_DRIFT` | 迁移后码点偏移被改写或截断 |
| `E_SEMANTIC_INCOMPLETE` | v1 等低表达能力格式无法还原 v2/v3 语义 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖新版本或乐观版本/校验和过期 |
| `E_SNAPSHOT_CORRUPT` | 快照 envelope 损坏或内容校验和不符 |
| `E_STAGE_BUSY` | 同一缓存键已有非阻塞独占写入者 |
| `E_INCONSISTENT` | 跨阶段集合、引用唯一性、保留/剔除集合或版本核对失败 |

## 实际验证结果

本工作区已执行：

```text
python3 -m unittest discover -s tests -v
Ran 30 tests in 4.462s
OK

python3 -m cqir verify
all_passed: true
```

关键结果：

```text
baseline byte hash:    37743006868c64ea1cd0584d09a0fb0e981654de3c4dd2beebfcfa54a401c475
incremental byte hash: 55e5c5f928bcdaf7e8259a069e66bf5bc0dbff536fff1c8c6c64161890c62a63
fresh replay hash:     55e5c5f928bcdaf7e8259a069e66bf5bc0dbff536fff1c8c6c64161890c62a63
incremental == fresh:  true
snapshot versions:     doc_version_table=2, citations_snapshot=2, answer_snapshot=2
```

全部自检项：

- `baseline_selfcheck`
- `cross_format_arbitration`
- `roundtrip_all_formats`
- `incremental_selfcheck`
- `incremental_equals_fresh`
- `snapshot_versions_monotonic`
- `snapshot_fault_probes`
- `business_fault_probes`
- `parallel_process_pool_equal`

异常探针实际返回：

```text
E_SPAN_OUT_OF_RANGE
E_CITE_DUP
E_FORMAT_VERSION_UNSUPPORTED
E_FORMAT_CORRUPT
E_SEMANTIC_INCOMPLETE
E_INCONSISTENT
E_SNAPSHOT_ROLLBACK
E_SNAPSHOT_CORRUPT
E_STAGE_BUSY
```

持久化产物：

```text
artifacts/demo_result.json
artifacts/demo_summary.json
artifacts/demo_verification.md
artifacts/answers/baseline.conv-002.turn-1.v1.txt
artifacts/answers/baseline.conv-002.turn-1.v2.txt
artifacts/answers/baseline.conv-002.turn-1.v3.txt
artifacts/answers/incremental.conv-002.turn-1.v1.txt
artifacts/answers/incremental.conv-002.turn-1.v2.txt
artifacts/answers/incremental.conv-002.turn-1.v3.txt
```

本地 HTTP 验证：

```text
GET  /api/health      -> {"ok":true,"service":"cqir"}
GET  /api/bootstrap   -> 9 queries, 7 documents
POST /api/demo        -> all_passed=true, incremental_equals_fresh=true
GET  /                -> 本地控制台页面
```

`/api/demo` 使用非阻塞演示锁；重复请求不会并发修改同一快照目录。
