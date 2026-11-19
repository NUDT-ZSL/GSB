# 偏好对增量数据准备诊断工作台

面向 LLM 偏好对齐与奖励建模数据准备，本地确定性生成偏好样本，并把
“质量清洗 → 近重复检测 → 对级一致性判定 → 配比采样”实现为有向无环计算图。
每个节点产物写入内容寻址缓存；输入、上游产物或节点参数变化时，只重算依赖这些变化的节点。
工作台同时提供静态 HTML 与本地 HTTP 诊断页面。

项目不依赖容器、外部模型、线上数据或网络时钟。Python 版本要求 `>=3.11`，
运行时依赖 NumPy，测试依赖 pytest。

## 快速开始

```bash
# 使用现成的 uv（也可用 python3.11/3.12 -m venv .venv）
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[test]"

# 或：
# python3.12 -m venv .venv
# . .venv/bin/activate
# python -m pip install -e ".[test]"

source .venv/bin/activate
pref-workbench --help
```

生成固定种子样本与配置，并运行首次全量快照：

```bash
pref-workbench generate --out data --seed 20261120 --count 36
pref-workbench run \
  --samples data/samples.jsonl \
  --config data/config.json \
  --stage .stage \
  --mode full
```

修改一位标注者的样本后执行增量提交：

```bash
pref-workbench mutate \
  --samples data/samples.jsonl \
  --out data/samples.corrected.jsonl \
  --pair-id p0001 \
  --chosen-suffix " [annotator correction]"

pref-workbench run \
  --samples data/samples.corrected.jsonl \
  --config data/config.json \
  --stage .stage \
  --mode incremental \
  --json
```

调整节点参数：

```bash
pref-workbench config-set \
  --config data/config.json \
  --out data/config.near-068.json \
  --path near_duplicate.similarity_threshold \
  --value 0.68

pref-workbench config-set \
  --config data/config.json \
  --out data/config.sampling-seed.json \
  --path sampling.seed \
  --value 424242
```

运行自检并生成诊断页面：

```bash
pref-workbench selfcheck --stage .stage
pref-workbench dashboard --stage .stage --out artifacts/dashboard.html
pref-workbench serve --stage .stage --host 127.0.0.1 --port 8765
```

浏览器访问 `http://127.0.0.1:8765/`。页面不加载外部 CDN 资源。

## 一键验收

```bash
source .venv/bin/activate
pytest -q
pref-workbench demo --workspace .
```

演示会生成：

- `artifacts/verification.json`：14 项验证明细；
- `artifacts/dashboard.html`：最终快照的可点击诊断页面；
- `artifacts/demo/`：样本、配置、各版本 stage 与全量对照 stage。

当前环境实际验证结果：

- Python `3.12.13`、NumPy `2.5.5`、pytest `9.2.0`；
- `pytest`：`22 passed`；
- `pref-workbench demo`：`14/14` 检查通过；
- 最终演示快照为 `v6`，数据集校验和
  `sha256:0b6085ed34e45962a0b70d8f068848a4ff6e11648ee00a9044207fe5e57791be`；
- 标注修正后的增量结果与全新 stage 全量重跑：四个节点的缓存键、payload hash、
  数据集校验和及内容寻址 JSONL 字节均一致。

## 项目结构

```text
src/prefworkbench/
  generator.py      固定种子样本/配置生成、样本读取与修正
  config.py         默认配置、配置校验、节点参数切片
  nodes.py          四个确定性业务节点
  graph.py          DAG 拓扑、cache key 构造
  cache.py          不可变内容寻址缓存条目与校验
  store.py          非阻塞 stage 锁、清单/数据快照、版本化整体替换
  engine.py         图执行、增量复用、失效传播、资源控制
  analysis.py       pair 级状态 diff
  selfcheck.py      跨模块一致性自检
  diagnostics.py    诊断数据与 HTML 渲染
  server.py         本地 HTTP API
  demo.py           一键验收流程
tests/              22 个 pytest 回归测试
```

## 确定性设计

所有缓存键、内容哈希和快照均使用同一套规范 JSON：

- 对象键排序；
- `ensure_ascii=False`；
- 紧凑分隔符；
- 禁止 NaN；
- 所有集合在输出前排序；
- 不使用 `hash()`、系统时间、进程随机状态或未声明随机数；
- 样本生成显式使用配置中的固定种子；
- MinHash 签名和采样排序显式使用配置中的固定种子。

中间节点不包含版本号或时间戳；版本号只存在于清单、缓存索引和版本化包装层。
因此相同输入/配置下，节点 payload hash 与内容寻址数据集 JSONL 逐字节稳定。

## 计算图语义

### 1. 质量清洗 `quality_clean`

对每个 pair 的 chosen/rejected 分别计算：

- 长度分：`min(1, len(reply) / ideal_length_chars)`；
- 重复率分：`1 - 重复 n-gram 比例`；
- 噪声分：`1 - 非法字符比例`，允许字符与标点由配置决定；
- 加权分：按 `quality_clean.weights` 归一化；
- 质量标记惩罚：读取 `flag_penalty`，且 `blocked_flags` 直接剔除；
- 质量档：按 `tier_thresholds` 分为 `high` / `medium` / `low`。

低于 `min_score` 或被阻断的样本进入 dropped 集，并记录原因。

### 2. 近重复检测 `near_duplicate`

- 使用配置中的 Unicode 分词正则；
- 文档由 prompt、chosen、rejected 组成；
- 每个文档生成固定长度 MinHash 签名；
- 相似度为签名槽位相等比例；
- 相似度达到 `similarity_threshold` 的 pair 使用确定性 union-find 归组；
- 相似对按 `(-similarity, left_pair_id, right_pair_id)` 排序；
- 组 ID 由组内最小 `pair_id` 生成；
- 最近邻同分时选择较小的 `pair_id`。

候选比较次数超过 `max_comparisons` 时：

- `on_limit=error`：返回 `E_RESOURCE_LIMIT`；
- `on_limit=degrade`：按 pair_id 字典序比较前 N 对，并记录
  `NEAR_DUPLICATE_COMPARISON_TRUNCATED`。

### 3. 对级一致性 `pair_consistency`

- prompt 完全相同或 token Jaccard 相似度达到门限时归入同一 prompt 组；
- 组内按精确 prompt、规范化回复文本检查角色；
- 同一回复在同一 prompt 下同时出现在 chosen 与 rejected 中时，记录冲突；
- 冲突包含 `conflict_group_id`、prompt 摘要、response 摘要、pair_id 与角色；

冲突策略：

- `error`：返回 `E_PAIR_CONFLICT`，附带 pair_id 与冲突组 ID；
- `keep_and_flag`：保留涉及样本并写入冲突记录；
- `drop_conflicting_pairs`：显式剔除冲突 pair，同时保留冲突记录。

### 4. 配比采样 `allocation_sampling`

- 随机性只来自 `sampling.seed`；
- 候选排序键为 `SHA256(seed:pair_id)`，哈希相同再按 `pair_id`；
- 第一阶段同时尊重 annotator_group 与 quality_tier 目标；
- 第二阶段以总目标为最高优先级，必要时填充超出单项配额；
- 清单记录每项目标、实际量、可用量、状态和原因；
- 欠配原因包括 `INSUFFICIENT_TOTAL_INVENTORY`、
  `INSUFFICIENT_ELIGIBLE_INVENTORY`、`NO_ELIGIBLE_INVENTORY`、
  `TOTAL_TARGET_PRIORITY`；
- 输出样本按 `pair_id` 排序，配额明细按类别与名称排序。

## 缓存键

每个节点缓存键由完整组件列表重算：

```text
key_components = [
  implementation_version,
  node_schema_version,
  node,
  raw input digest 或上游 payload hash,
  node_parameters
]
cache_key = SHA256(canonical_json(key_components))
```

具体地：

- `quality_clean`：全部样本摘要 + 质量参数；
- `near_duplicate`：质量节点 payload hash + 近重复参数；
- `pair_consistency`：近重复节点 payload hash + 一致性参数；
- `allocation_sampling`：一致性节点 payload hash + 采样参数。

每个缓存条目还包含：

- `payload_hash = SHA256(canonical_json(payload))`；
- `entry_checksum = SHA256(canonical_json(entry 去掉自身 checksum))`；
- `created_version`；
- 完整 `key_components`。

诊断页支持点击节点查看这些字段，并可手工重算 `cache_key` 与 `payload_hash`。

## 增量失效

| 变化 | 预期行为 |
| --- | --- |
| 某标注者样本修正 | 原始样本摘要变化；质量及全部下游重算 |
| 质量权重变化 | 质量及全部下游重算 |
| 近重复门限变化 | 质量缓存复用；近重复、一致性、采样重算 |
| prompt 一致性参数变化 | 质量与近重复复用；一致性、采样重算 |
| 采样种子/配额变化 | 前三节点复用；仅采样重算 |
| 无输入变化 | 四节点全部命中；版本递增但数据集校验和不变 |

清单输出：

- `reused_nodes`；
- `recomputed_nodes`；
- `invalidated_nodes`；
- `affected_nodes`；
- 每节点 `affected_pair_ids` 与 `changed_pair_ids`；
- 原始样本 added/removed/changed pair_id；
- 失效原因列表。

`changed_pair_ids` 表示节点产物确实发生 pair 级变化的集合；
`affected_pair_ids` 还包含参数变化影响范围和上游传播范围。

## 版本、校验与并发

stage 布局：

```text
.stage/
  current.json                    # 完整当前清单，原子替换
  manifests/v000001.json          # 不可变版本快照
  cache/objects/<node>/<key>.json # 不可变内容寻址条目
  cache/indexes/v000001.json      # 版本化缓存索引，整体替换
  datasets/dataset-v000001.json   # 带版本的数据集快照
  datasets/content/<sha>.jsonl    # 纯内容寻址数据行，可逐字节比较
  errors.json                     # 最近业务错误码
```

- 每次成功提交版本单调加一；
- `expected_base_version` 不匹配时返回 `E_STALE_VERSION`；
- 旧版本不能覆盖当前版本；
- 当前清单、数据集和缓存索引均先写临时文件，再 `os.replace` 整体替换；
- 缓存对象损坏、payload 哈希不符、条目校验和不符时返回
  `E_CACHE_CORRUPT`，并带节点名、版本号和损坏类别；
- 不消费部分可解析数据。

提交锁使用 `fcntl.flock(LOCK_EX | LOCK_NB)` 加进程内非阻塞锁。
并发提交的后到者立即收到 `E_STAGE_BUSY`。

## 资源限制

配置项：

- `near_duplicate.max_comparisons`：候选比较次数；
- `resources.max_manifest_bytes`：清单编码后字节数；
- `resources.max_parallel_nodes`：请求的并行重算节点上限；
- `near_duplicate.on_limit` / `resources.on_limit`：`error` 或 `degrade`。

超限时：

- 严格模式返回 `E_RESOURCE_LIMIT`；
- 降级模式写入类别，例如 `NEAR_DUPLICATE_COMPARISON_TRUNCATED`、
  `PARALLELISM_CAPPED`、`MANIFEST_DETAIL_TRUNCATED`。

本实现的计算图是链式 DAG，实际执行按拓扑顺序，因此有效并行度为 1；
`max_parallel_nodes` 仍作为提交前的并行请求准入与降级边界。

## 诊断页面

页面展示：

- 四节点拓扑与命中/重算状态；
- 每节点 cache key、payload hash、条目版本与 checksum；
- 点击节点展开完整键组件与受影响 pair_id；
- 失效传播、复用和重算覆盖；
- 对级冲突与冲突组；
- 配比目标、实际量、欠配原因；
- 资源降级类别；
- 最近业务错误码；
- 最新自检结果。

HTTP API：

- `GET /api/diagnostics`
- `GET /api/selfcheck`
- `GET /api/errors`

## 自检

`pref-workbench selfcheck` 校验：

- 拓扑无环且节点集合正确；
- 质量 retained/dropped 无交集；
- 采样 selected/unselected 无交集；
- 清单每个样本可追溯到质量分、质量档、签名组、相似度和 prompt 组；
- 复用/重算集合无交集且覆盖全部节点；
- 受影响节点被复用或重算覆盖；
- cache key 可由组件重算；
- payload hash、条目 checksum、版本化缓存索引一致；
- 数据集校验和、版本、内容寻址 JSONL 文件一致。

任一失败返回 `E_INCONSISTENT`，并列出类别与明细。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_CONFIG` | 配置缺失或取值非法 |
| `E_INPUT` | 样本/输入不可读或字段非法 |
| `E_PAIR_CONFLICT` | 同一 prompt 下 chosen/rejected 交叉矛盾 |
| `E_CACHE_CORRUPT` | 缓存或快照损坏、校验和不符 |
| `E_RESOURCE_LIMIT` | 资源上限触发且策略为严格 |
| `E_STAGE_BUSY` | 已有增量提交持有 stage 锁 |
| `E_STALE_VERSION` | 基于旧版本的提交请求 |
| `E_INCONSISTENT` | 跨模块一致性自检失败 |

## 实际验证

```text
$ pytest -q
......................                                            [100%]
22 passed

$ pref-workbench demo --workspace .
{
  "ok": true,
  "checks": [
    {"name": "deterministic_generation", "ok": true},
    {"name": "initial_full_recompute", "ok": true},
    {"name": "identical_input_all_cache_hit", "ok": true},
    {"name": "annotator_edit_incremental_equals_full", "ok": true},
    {"name": "near_threshold_prefix_reuse", "ok": true},
    {"name": "sampling_seed_only_recomputes_sampling", "ok": true},
    {"name": "quality_weight_invalidates_downstream", "ok": true},
    {"name": "strict_conflict_returns_E_PAIR_CONFLICT", "ok": true},
    {"name": "explicit_conflict_retention_is_recorded", "ok": true},
    {"name": "near_limit_degrade_is_marked", "ok": true},
    {"name": "near_limit_error_returns_E_RESOURCE_LIMIT", "ok": true},
    {"name": "corrupt_cache_returns_E_CACHE_CORRUPT", "ok": true},
    {"name": "stale_version_cannot_roll_back", "ok": true},
    {"name": "final_cross_module_selfcheck", "ok": true}
  ]
}
```

完整机器可读结果见 `artifacts/verification.json`。
