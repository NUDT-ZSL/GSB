# LayerForge

LayerForge 是一个零第三方运行时依赖的云原生分层构建与缓存裁决组件。它使用固定种子生成两代构建清单：

- v1：单文件 JSON，含步骤序号、基础层引用、文件动作、环境变量、构建参数和相对写入秒数。
- v2：分片 JSON + SQLite 元数据库，含规范层摘要、分片哈希、绝对写入毫秒、缓存代数、步骤键和失效墓碑。

组件完成两代清单校验合并、确定性层对象物化、精确/分桶采样缓存裁决、失效闭包传播、逻辑预算调度、SQLite 持久化、强杀恢复和多进程幂等提交。不依赖 Docker、镜像仓库、网络或真实容器运行时。

## 快速开始

要求 Python 3.10+。

```bash
python scripts/generate_samples.py --output samples
python -m layerforge judge --workdir .layerforge/demo --report-dir .layerforge/demo/reports
python -m layerforge build --workdir .layerforge/demo --report-dir .layerforge/demo/reports
python -m layerforge verify --workdir .layerforge/demo --report-dir .layerforge/demo/reports
```

一键验收：

```bash
bash scripts/acceptance.sh
```

该脚本会重新生成样本、运行 12 个测试，并验证强杀恢复、预算放弃、参数失效闭包和时钟回拨错误码。实际输出保存在 `artifacts/acceptance-run.txt`。

## CLI

所有命令均输出 JSON；完整报告默认写入 `<workdir>/reports/`。

- `generate`：生成确定性样本和 SQLite 元数据。
- `judge`：输出每步复用结论、来源版本、缓存代数、失效信息和采样证据。
- `build`：校验合并、调度物化、提交 SQLite 状态和摘要清单。
- `resume`：清理/识别半提交对象，只重放未完成部分。
- `status`：输出快照版本、步骤阶段、时钟水位、调度水位和失效链。
- `verify`：重算摘要并检查对象、摘要清单、SQLite `integrity_check`，可与一次性运行目录逐字节对比。
- `worker`：额外的单步多进程 worker，用于验证同一步骤/父层只物化一次、同一缓存键只提交一次。

常用参数：

```bash
python -m layerforge build \
  --sample-dir samples \
  --workdir .layerforge/run \
  --report-dir .layerforge/run/reports \
  --concurrency 2 \
  --round-budget 20 \
  --set-arg BUILD_CHANNEL=canary

python -m layerforge judge --invalidate app-layer
python -m layerforge verify --reference-dir .acceptance/one
```

崩溃注入仅用于本地验证：

```bash
python -m layerforge build \
  --workdir .layerforge/crash \
  --crash-step final-image \
  --crash-at object_renamed   # temp_object | object_renamed | materialization_committed | cache_commit

python -m layerforge resume --workdir .layerforge/crash
```

## 确定性输出

- 规范化 JSON 使用键排序、紧凑分隔符计算逻辑摘要。
- 层对象使用两空格缩进、键排序和末尾换行序列化；层摘要是对象字节的 SHA-256。
- 对象路径为 `objects/sha256/<前两位>/<完整摘要>.json`。
- 写入采用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。
- 所有调度时间只来自固定时钟基数和步骤 `work_cost_ms`，不读取墙钟。
- `digests.json` 与对象目录在不同运行目录、崩溃恢复前后逐字节一致。

## 采样漏判边界

候选数超过 `exact_candidate_threshold` 或本地层对象数超过 `exact_object_threshold` 时，从精确扫描切换到 SHA-256 分桶采样。样本默认：

- 16 个摘要桶；
- 确定性选择 10 个桶；
- 单匹配候选的先验漏判上界为 `(16-10)/16 = 0.375`；
- 同时输出组合数学边界、无分布边界、目标桶是否命中以及是否升级为精确扫描。

该概率边界依赖“固定种子选择等价于均匀桶抽取”的声明模型；账本同时保留分布无关上界 `1.0`，避免把确定性样本误述为对抗分布下的保证。

## 失效闭包

缓存键由以下字段规范化后计算：

- `step_id`
- 层对象摘要
- 父层摘要
- 额外依赖摘要
- 解析后的构建参数
- 解析后的环境变量

参数变化只直接命中声明 `${PARAM}` 或 `arg_refs` 引用该参数的步骤，随后沿依赖边传播到全部下游。未受影响的上游仍显示 `reuse`，其对象字节不变。失效链持久化触发源、根步骤、受影响步骤、前后代数和链 ID。

## 持久化与并发

SQLite 状态库保存：

- 当前快照指针和不可变快照报告；
- 步骤阶段与当前摘要/键；
- `(step_id, parent_digest)` 物化记录；
- 内容寻址对象索引；
- 缓存键提交记录；
- 失效链；
- 调度轮次、事件和水位；
- 逻辑时钟水位。

每批快照使用 `expected_version -> version+1` 的 CAS 指针替换。`fcntl.flock` 保护 `mat-<step>-<parent>` 和 `cache-<key>` 临界区，SQLite `BEGIN IMMEDIATE` 与 busy timeout 负责多进程事务。

## 业务错误码

| 场景 | 错误码 | CLI 退出码 |
| --- | --- | ---: |
| 路径越界 | `E_PATH_ESCAPE` | 20 |
| 基础层/依赖缺失 | `E_BASE_LAYER_MISSING` | 21 |
| 缓存代数回退 | `E_GENERATION_ROLLBACK` | 22 |
| 分片哈希不匹配 | `E_SHARD_HASH_MISMATCH` | 23 |
| 采样摘要冲突 | `E_SAMPLE_DIGEST_CONFLICT` | 24 |
| 逻辑时钟回拨超容差 | `E_CLOCK_ROLLBACK` | 25 |
| 两代层字段冲突 | `E_LAYER_CONFLICT` | 26 |
| 快照版本冲突 | `E_VERSION_CONFLICT` | 28 |
| 轮次预算放弃 | `E_BUDGET_ABANDONED` | 30 |

所有错误 JSON 都包含 `error_code`、`message` 和可诊断 `details`。

## 代码结构

- `layerforge/manifestio.py`：v1/v2 校验、SQLite 元数据检查、跨版本合并。
- `layerforge/planner.py`：依赖图、参数解析、摘要和缓存键计划。
- `layerforge/layers.py`：动作应用、层规范和原子对象写入。
- `layerforge/cachejudge.py`：精确扫描、分桶采样和漏判边界。
- `layerforge/invalidation.py`：失效闭包和代数账本。
- `layerforge/runtime.py`：调度、并行 worker、崩溃点、恢复和校验。
- `layerforge/store.py`：SQLite 快照/状态事务。
- `layerforge/samplegen.py`：固定种子样本生成器。
- `tests/test_layerforge.py`：12 个端到端与边界测试。
