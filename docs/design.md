# 技术设计说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `generator.py` | 显式笔画规则、确定性手写抖动、样本/原型生成 |
| `models.py` | 不可变配置、样本、原型、门限与版本校验 |
| `pipeline.py` | 纯归一化、特征、距离、判决、指标及 spawn worker |
| `cache.py` | 内容寻址磁盘缓存、缓存自校验、有界特征 LRU |
| `snapshots.py` | 完整版本快照、校验和、回退和损坏保护 |
| `service.py` | 增量执行计划、节点集合账本、追溯重放、自检 |
| `cli.py` | 本地确定性验收流程与 JSON 报告 |

纯流水线函数不读取时钟、环境变量或未声明随机源。父进程负责缓存读写；worker 只接收不可变任务并返回带内容哈希的产物。

## 2. 节点与账本

节点 ID 形式：

```text
feature:<image_id>
distance:<image_id>:<class_id>
decision:<image_id>
metrics
```

每次执行维护：

- `node_keys`：当前每个节点的缓存键和可独立核对的 `key_parts`；
- `recomputed_nodes`：本次缺失缓存并重新计算的节点；
- `reused_nodes`：本次从内存或磁盘缓存加载的节点；
- `affected_nodes`：由配置/样本差异推导出的应失效节点。

自检要求 `recomputed ∩ reused = ∅`、`affected ⊆ recomputed ∪ reused`、`node_keys = recomputed ∪ reused`。

## 3. 内容哈希

规范序列化使用：

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

产物先对除去 `content_hash` 的载荷计算 SHA-256，再写入 `content_hash`。缓存条目再对完整产物计算一次 `artifact_sha256`。因此：

1. 可验证节点载荷是否完整；
2. 可验证缓存条目是否指向正确产物；
3. 可仅凭 `key_parts` 重算缓存键；
4. 下游可用上游产物的 `content_hash` 作为真实版本。

## 4. 增量执行

校准先比较旧/新快照的语义差异：

- 归一化校验和变化：全部 feature 失效；
- 距离度量变化：全部 distance 失效；
- 某类原型成员版本/哈希变化：仅该类 distance 失效；
- 原型集合版本/校验和变化：全部 decision 失效；
- 门限校验和变化：全部 decision 失效；
- 判决或标签/不确定标记变化：metrics 失效。

执行器仍以实际缓存键为最终依据。键未变则允许复用；键变化且缓存不存在则重算。全量比较不读取缓存，而是按相同纯函数从样本、特征、原型和门限重新构造所有判决，随后比较规范字节。

## 5. 追溯与重放

判决中的 `node_path` 指向构造它的 feature、10 个 distance 和 decision 缓存键。严格重放要求当前四类版本与记录一致，然后用记录特征独立重算距离，并仅由距离和门限重算判决。

历史重放允许加载旧版本快照，但会验证：

- 快照 envelope 校验和；
- 快照载荷版本；
- 载荷本身的内容校验和与判决记录一致；
- 特征缓存键与 `feature_version` 一致；
- 每个距离缓存哈希与判决记录一致；
- 独立重算的距离、类别、拒识标记和置信度与旧记录一致。

因此“重放一致”不是原样复制旧判决，而是基于历史依赖重新计算后的相等。

## 6. 资源降级

并行请求超过 `max_parallel_nodes` 时：

- 默认降级到串行，记录 `{"category":"parallelism", ...}`；
- `allow_parallel_degrade=False` 时直接 `E_RESOURCE_LIMIT`。

特征驻留采用 LRU：

- `memory_policy="error"`：超限 `E_RESOURCE_LIMIT`；
- `memory_policy="evict"`：驱逐旧特征并记录 `{"category":"feature_memory", ...}`。

降级标记只存在于执行报告中，不进入判决内容哈希；降级前后的判决规范字节必须相同。无论并行、串行、驱逐与否，节点规划范围不缩减。

## 7. 快照不变量

快照目录按 `snapshots/<name>/vNNNNNN.json` 保存完整 envelope。读取最新版本并验证：

- 路径版本、envelope 名称/版本一致；
- 完整载荷重新计算校验和一致；
- JSON 可解析、载荷为对象。

替换先验证所有待替换快照，再写入完整 envelope。低于当前版本返回回退错误；同版本不同内容返回损坏错误。指标快照每发生变化时生成新的单调版本。

## 8. 可扩展点

- 新距离度量：扩展 `DistanceMetric` 允许值和 `compute_distance_value()`。
- 新特征投影：提升 `projection_bins`/`cell_grid_size` 并保持 `feature_dim` 等式。
- 新置信规则：保留“拒识不得为 high”的合法性校验。
- 分布式缓存：保持 `key_parts`、内容哈希和缓存条目自校验协议即可替换存储后端。
