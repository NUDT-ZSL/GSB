# 本地跨模态图文对齐与增量检索管线

这是一个从零实现的本地确定性图文检索管线。它把图像编码、文本编码、跨模态对齐、分块量化索引和 top-k 检索建成五级 DAG，并用内容寻址缓存、分片失效和血缘审计处理增量更新。

整个演示不访问网络，不使用 Docker。样本像素由 `image_seed` 和 `image_shape` 本地生成；文本由受控词表确定性池化；码本、投影参数、样本清单和异常样本都在 `config/`、`data/` 中。

## 快速开始

目标运行时是 CPython 3.11 + NumPy 1.26.4：

```bash
uv python install 3.11
uv venv --python 3.11 .venv311
uv pip install --python .venv311/bin/python -r requirements.txt
uv pip install --python .venv311/bin/python pytest
```

Windows 原生环境可使用：

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install -r requirements.txt
```

运行完整验收演示：

```bash
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli demo \
  --root artifacts/demo \
  --report artifacts/report.json \
  --workers 2
```

报告位于 `artifacts/report.json`，最终增量与全量检索结果分别位于：

- `artifacts/demo/results_incremental.json`
- `artifacts/demo/results_fresh_full.json`

常用命令：

```bash
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli init --root .
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli build-full \
  --data data/samples.jsonl --config config/pipeline.json \
  --vocab config/vocab.json --codebook config/codebook_v1.json \
  --cache artifacts/run/cache --state artifacts/run/state --workers 2
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli build-incremental \
  --data data/samples_updated.jsonl --config config/pipeline.json \
  --vocab config/vocab.json --codebook config/codebook_v1.json \
  --cache artifacts/run/cache --state artifacts/run/state --expected-version 1
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli verify \
  --cache artifacts/run/cache --state artifacts/run/state
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli replay \
  --cache artifacts/run/cache --state artifacts/run/state
PYTHONPATH=src .venv311/bin/python -m cm_pipeline.cli inspect-key \
  --cache artifacts/run/cache --key <64位缓存键>
```

## 计算图与数据

图结构由 `config/pipeline.json` 的 `graph.nodes`、`graph.edges` 和 `entrypoint` 决定：

```text
image_encoder:<pair> ──┐
                       ├──> align:<pair> ──text_aligned──> index_build:block-<n>
text_encoder:<pair> ───┘         │
                                 └──image_aligned──> retrieve:<query_pair>
                                                       ^
                           所有 index_build 分块 ──────┘
```

节点实例分别为：

- `image_encoder:<pair_id>`
- `text_encoder:<pair_id>`
- `align:<pair_id>`
- `index_build:block-<0..N-1>`
- `retrieve:<pair_id>`

样本 JSONL 字段为 `pair_id`、`image_seed`、`image_shape`、`text`、`text_lang`、`caption_revision`、`embed_dim`、`modality_mask`。`modality_mask` 是 `["image"]`、`["text"]` 或 `["image","text"]` 的无重复子集。当前固定图像尺寸为 `24x24x3`，分块大小为 `4x4`，编码维度为 16。

确定性约束：

- 像素、文本 token 向量、固定投影矩阵都来自显式 seed 和 SHA-256 字节流。
- 核心矩阵乘法使用逐元素乘加求和，不依赖 BLAS 线程归约顺序。
- 样本按 `pair_id` 排序；所有集合、字典和码本分块在输出前排序。
- 浮点数组用固定小端 dtype、固定 shape 和 C-order 原始字节编码。
- 结果、缓存键和报告不写入时间戳。

## 内容寻址缓存

每个缓存条目位于：

```text
<cache>/objects/<key[:2]>/<key>/
  meta.json
  payload.bin
```

`key = sha256(canonical_json(key_components))`。组成项包括节点类型、节点实例、pair_id、节点输入摘要、上游产物哈希、节点参数、词表版本/校验和以及适用的码本分块版本/校验和。

`payload.bin` 使用自定义的无 ZIP 元数据格式保存数组和 JSON 元数据；`product_hash = sha256(payload.bin)`。读取时会同时核对：

- 已存组成的规范化 JSON 是否能重算出同一条目 key；
- 实际 payload 字节是否能重算出 `product_hash`。

`inspect-key` 会返回完整组成项，用于诊断“为什么该缓存键变化”。

## 增量失效

增量提交为每个新实例重新构造缓存键。键存在则复用，键不存在才重算，因此不依赖“猜测哪些节点变了”。典型效果如下：

| 修改 | 重算范围 | 可复用范围 |
| --- | --- | --- |
| 单对文本/`caption_revision` | 该对 `text_encoder`、`align`，该对所在索引分块，全部 `retrieve` | 其他编码器、其他索引分块 |
| 单对 `image_seed` | 该对 `image_encoder`、`align`，该对 `retrieve` | 文本编码器、所有索引分块、其他查询 |
| 对齐投影参数 | 全部 `align`、全部索引分块、全部检索 | 全部图像/文本编码器 |
| 码本单块内容升级 | 该码本块对应索引分块、全部检索 | 未变化索引分块、全部编码与对齐 |
| 码本全局版本提升但分块内容不变 | 全部检索 | 全部编码、对齐和索引分块 |

索引按 `sha256(pair_id) % num_shards` 分片。每个码本分块保存 `version` 和内容 `checksum`；未变化分块保留旧有效版本，因此即使全局码本升级，也可以直接复用旧索引分块。

每次提交都生成不可变的新快照。已完成版本不会被覆盖；旧 `expected_version`、码本版本回退、词表内容同版本变化都会返回 `E_SNAPSHOT_ROLLBACK`。提交锁使用 `O_CREAT|O_EXCL`，第二个并发提交立即返回 `E_STAGE_BUSY`，不等待。

## 追溯、重放与码本一致性

每条命中记录查询级依赖血缘，而不是携带整份数据集摘要；这样单张无关图像变化时，依赖未变的检索产物仍可安全复用，而真正变化的查询缓存键会被拒绝。

每条命中记录：

- 查询与目标节点路径；
- 图像编码、查询对齐、目标文本编码、目标对齐、索引分块、检索节点的缓存键；
- 词表版本/校验和；
- 全局码本版本/校验和、索引分块编号/版本/校验和；
- 查询向量摘要、码字、残差摘要、重建向量摘要；
- 本次快照的配置、样本、图、词表、码本血缘摘要。

`verify` 独立检查：

- DAG 无环；
- 缓存键与产物哈希可互算；
- 重算集合与复用集合无交集，并集覆盖全部节点实例；
- 每条命中在 `(query_pair_id, rank)` 上唯一；
- top-k 分数降序、同分按 `target_pair_id` 升序；
- top-k 分数和排序可由查询向量、分块码字与残差重新算出；
- 结果文件摘要与快照记录一致。

任意偏差返回 `E_VERIFY_FAIL`，并给出首个偏差的 `category` 和 `location`。正常构建中若码字、残差或 top-k 无法由码本分块重算，则返回 `E_INCONSISTENT`，并在 `details` 中包含 `block_id` 与 `codebook_version`。

## 业务错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_DUP_PAIR` | 重复 `pair_id` |
| `E_SEED_SHAPE_MISMATCH` | 非法 seed，或 `image_shape` 与配置固定形状不一致 |
| `E_TEXT_EMPTY` | `text` 为空或只有空白 |
| `E_MASK_INVALID` | `modality_mask` 非法 |
| `E_CONFIG_INVALID` | `embed_dim <= 0`、配置/词表/码本校验失败、图成环等 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本提交、快照回退、词表/码本版本回退 |
| `E_LINEAGE_BROKEN` | 旧缓存键命中被要求用于当前血缘，或依赖版本不一致 |
| `E_STAGE_BUSY` | 已有提交持有提交阶段锁 |
| `E_RESOURCE_LIMIT` | 重算 pair 数、码本驻留估算或缓存条目数超限 |
| `E_INCONSISTENT` | 码字、残差、重建或 top-k 无法独立复算 |
| `E_VERIFY_FAIL` | 审计发现首个一致性偏差 |

## 资源限制

`config/pipeline.json` 的 `resources` 支持：

```json
{
  "max_recompute_pairs": 10000,
  "max_codebook_bytes": 1048576,
  "max_cache_entries": 100000,
  "on_limit": "error"
}
```

`on_limit` 为 `error` 时超限返回 `E_RESOURCE_LIMIT`；为 `degrade` 时，pair 重算和码本驻留超限会在报告中标记 `pair_recompute_pairs` 或 `codebook_memory` 降级类别，并继续按分块/分块流式方式完成。缓存条目上限始终硬失败，因为淘汰旧条目会破坏历史快照重放。

## 实际验证结果

最终报告由 Python 3.11.18、NumPy 1.26.4 在 Linux/WSL2 上生成，完整结论在 `artifacts/report.json`：

- 初始全量：51 个节点重算，0 个复用；3 个索引分块均构建。
- 文本对 `PAIR-0002` 与图像 seed `PAIR-0005` 同时修改：17 个节点重算、34 个复用；只重算索引 block 2，block 0/1 复用。
- 码本 v2 只修改 block 1：13 个节点重算、38 个复用；只重算 block 1，block 0/2 复用。
- 对齐投影升级：27 个节点重算、24 个复用；全部图像/文本编码器复用。
- 增量最终结果与全新全量重建均为 100,875 字节，SHA-256 同为：
  `dc565f8c385da5db3999e4132892bd65c0c7ae662e6095acd2b8802b0ce71bf4`。
- 独立验证的 7 类检查和 33 条命中重放全部通过。
- 过期命中返回 `E_LINEAGE_BROKEN`；旧版本提交和码本回退返回 `E_SNAPSHOT_ROLLBACK`；并发提交返回 `E_STAGE_BUSY`。
- 重复 pair、shape 不匹配、空文本、非法 mask、非法 embed_dim 分别返回 `E_DUP_PAIR`、`E_SEED_SHAPE_MISMATCH`、`E_TEXT_EMPTY`、`E_MASK_INVALID`、`E_CONFIG_INVALID`。
- 单元测试：`15 passed`，覆盖全量/增量对拍、进程池确定性、码本分块复用、历史重放、缓存篡改、资源上限、并发和回退。

平台说明：当前是 WSL2 宿主上的 Linux 运行时，Windows interop 不可执行，因此不能声称已在原生 Windows Python 上实测。代码使用 `pathlib`、`os.replace`、`O_EXCL` 文件锁和带 CLI 主保护的 `multiprocessing`，并提供 Windows 启动命令；原生 Windows 验收需在 Windows 主机重新运行同一 `demo` 命令。

## 测试

```bash
PYTHONPATH=src .venv311/bin/python -m pytest -q
```

测试文件位于 `tests/test_pipeline.py`，确定性样本和异常副本由 `src/cm_pipeline/fixtures.py` 生成。
