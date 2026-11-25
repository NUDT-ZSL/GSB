# LayerForge 设计说明

## 1. 输入与统一视图

固定种子生成器 `layerforge/samplegen.py` 产出：

- `samples/v1_manifest.json`
- `samples/v2_manifest.json` 与 `samples/v2/shards/*.json`
- `samples/v2_metadata.sqlite3`
- `samples/build_config.json`、`samples/build_params.json`
- `samples/expectations.json`

载入顺序：

1. 校验 v1 序号、引用、路径、键值和相对秒数。
2. 校验 v2 索引哈希、分片字节哈希、SQLite 分片哈希和代数单调性。
3. 规范化两代 `semantic_core` 并比较摘要。
4. 将 v1 相对秒换算为 `conversion_base_ms + seconds * 1000`，记录标度 `1000ms/s`、`exact_integer_multiplication`、舍入方向 `none` 和原始值。
5. 合并缓存来源。最新来源先比较声明逻辑时钟；时钟相同则比较格式优先级（v2=20，v1=10）；同来源多行再比较缓存代数。

## 2. 层对象与缓存键

每个计划步骤包含：

- 解析后的动作；
- 父层摘要和额外依赖摘要；
- 解析后的参数/环境变量；
- 结果文件元数据表；
- diff 的 `added/modified/deleted`；
- 固定逻辑写入时间和换算记录。

层对象字节是键排序、两空格缩进 JSON 加换行。层摘要为这些字节的 SHA-256，对象路径按摘要分片：

```text
objects/sha256/<digest[7:9]>/<digest>.json
```

缓存键输入：

```json
{
  "step_id": "...",
  "layer_digest": "sha256:...",
  "parent_digest": "sha256:...",
  "dependency_digests": [],
  "resolved_args": {},
  "resolved_env": {}
}
```

全局参数不会无条件进入所有步骤；只有通过 `${NAME}` 或 `arg_refs` 显式引用参数的步骤才形成直接失效根。

## 3. 自适应采样

`exact_candidate_threshold=12`、`exact_object_threshold=24`。任一计数超阈值即采样。

采样把候选的内容摘要映射到 16 个桶，并用固定种子的 SHA-256 排序选择 10 个桶。账本输出：

- 实际模式和触发表达式；
- 被否决模式及原因；
- 目标桶、是否抽中、各桶候选数；
- 每个比较项的来源和代数；
- 单匹配先验上界 `(B-S)/B`；
- 组合数学边界 `C(B-1,S)/C(B,S)`；
- 分布无关边界 `1.0`。

若采样未命中且先验边界高于 `max_miss_bound`，自动升级全量精确扫描。

## 4. 失效闭包

失效根来自：

- 解析参数变化且步骤显式引用该参数；
- `--invalidate STEP`；
- 裁决器找不到有效命中的额外未命中步骤。

从根沿依赖图下游做一次闭包。闭包内每一步记录：

- 链 ID；
- 触发源；
- 根步骤和完整受影响集合；
- 上一代和新一代；
- 链逻辑时钟。

参数变化会改变层规范，因此受影响层摘要和缓存键也随之改变；纯缓存失效可能保持摘要/键不变，但裁决结论仍为 `invalidated_rebuild`，重建后靠内容寻址和缓存键唯一约束幂等提交。

## 5. 调度与预算

每轮：

1. 找出依赖已完成的步骤。
2. 按 `(seq, step_id)` 拓扑排序。
3. 顺序准入，直到达到并发上限或全局聚合逻辑预算。
4. 并发上限导致未准入：`not_started_within_budget / concurrency_limit_for_round`。
5. 预算不足：当前步骤及其后就绪步骤记为 `abandoned_budget`。
6. 已准入步骤在线程池中全部执行并等待；异常记为 `failed_after_start`，不会被降级成未启动。

预算口径是一轮内所有准入步骤 `work_cost_ms` 的聚合工作时间；并行轮次的逻辑时钟推进取该批最大步骤耗时（关键路径）。所有耗时均来自清单/配置，不使用墙钟。

## 6. 持久化、锁与恢复

状态库：`<workdir>/state.sqlite3`。

关键唯一约束：

- `materializations PRIMARY KEY (step_id, parent_digest)`：同一步骤同一父层只有一条物化状态。
- `cache_entries.cache_key PRIMARY KEY`：同一键只提交一次。
- `build_pointer(id=1)`：当前快照版本。

提交流程：

1. `fcntl.flock mat-<step>-<parent>`。
2. 锁内复查 SQLite 和最终对象。
3. 写 `materializing` 记录。
4. 临时文件、fsync、`os.replace` 原子写对象。
5. 提交 `committed` 物化记录和对象索引。
6. `fcntl.flock cache-<key>`。
7. 插入唯一缓存键并更新步骤阶段。

恢复时扫描 `materializing`：

- 最终对象存在且摘要正确：补提交；
- 只有 `.tmp`：清理并重放；
- 物化已提交但缓存缺失：只补缓存提交。

快照提交使用 CAS：读取 `current_version`，事务中复查，插入 `version+1` 快照并更新指针；冲突时运行时重读重试。

## 7. 多进程

`layerforge worker --step STEP` 提供单步进程入口。两个相同 worker 并发时，文件锁保证第二个等待后看到已提交记录并返回 `reused_committed`；不同步骤使用不同锁，可真正并行。SQLite 唯一约束是第二道幂等防线。
