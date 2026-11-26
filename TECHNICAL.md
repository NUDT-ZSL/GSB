# 技术说明

## 1. 数据与确定性契约

统一内部表示为 `ThresholdConfig`，包含代数、快照修订、创建/更新时间、三个门限条目、墓碑、预算、候选计划数、调度并发数和精度档。

所有跨模块 JSON 使用以下规范：

- 对象键排序；
- UTF-8；
- 紧凑分隔符或固定两空格缩进；
- 禁止 NaN/Infinity；
- 浮点值统一 `round_half_even` 到 12 位小数；
- 哈希均基于规范化 JSON 的 SHA-256。

配置读写器拒绝重复 JSON 键，避免解析器“后值覆盖前值”掩盖冲突。

## 2. 配置读取路径

`sniff_format_version()` 是唯一版本分派入口：

1. 显式版本头优先；
2. 无头时检查 v1 必需字段集；
3. 再检查 v2 manifest 的 `shards + generation` 字段集；
4. 无法唯一判断返回 `E_VERSION_UNSUPPORTED`。

v1 强制要求 `thresholds` 精确包含 `image/joint/text`，所有运行字段必须存在且类型可解析。v1 中若出现 v2-only 元数据，不进行静默覆盖，而返回 `E_FORMAT_CORRUPT`。

v2 校验链：

```text
manifest 字段/版本
  -> metadata_checksum 对 SQLite 文件
  -> SQLite 元数据与 manifest 逐项相等
  -> shard sha256
  -> shard 版本、条目字段、修订与时间
  -> 时间戳单调性
  -> 统一模型不变量
```

## 3. 迁移

### v1 → v2

构造统一 v2 表示并生成 `ProvenanceRecord`：

- `created_at_ms` / `updated_at_ms`：由 v1 RFC3339 UTC 时间转换；
- `generation=1`：单文件快照规则；
- 快照与条目 `revision=0`：v1 无增量修订通道；
- `tombstones=[]`：v1 无墓碑通道；
- 条目时间：继承快照时间。

### v2 → v1

v1 可表达字段被投影并检查，不可表达字段输出 `DiscardRecord`：

- 快照代数；
- 快照增量修订号；
- 条目增量修订号；
- 分离的创建时刻；
- 非快照时刻的条目时间。

墓碑会改变失效语义，无法用 v1 表达，直接返回 `E_SEMANTIC_LOSS`。`strict_lossless=True` 时，任何显式丢弃也返回语义损失。

## 4. 路由算法

向量长度固定为 16。每个文本/图像/交叉向量由 SHA-512 固定块映射到 `[-1,1]`，不依赖进程随机状态。

相似度：

```text
cosine01(a,b) = clip((dot(a,b) / (||a||*||b||) + 1) / 2, 0, 1)
```

按配置精度档选用 numpy `float16/float32/float64`。三个相似度用于门限筛选；候选计划具有固定门限模态、门限裕量、基础权重和偏置。

N4 参数融合：

```text
gain[m]    = sim[m] - threshold[m] + 0.05
adjusted_w = plan_w[m] * (0.5 + 0.5*gain[m])
fused_w    = adjusted_w / sum(adjusted_w)
score      = sum(fused_w[m] * sim[m]) + bias
```

通过门限的候选按 `(-score, plan_id)` 确定性排序。若没有候选，输出 `fallback_dual_modality` 且业务码为 `E_NO_ROUTE`。

每个节点返回整数操作计数和舍入模式。决策序列不包含缓存状态，因此可做字节级重放比较。

## 5. 内容寻址缓存

键组件：

```json
{
  "cache_format_version": 1,
  "operation_contract": "mm-route-contract-v1",
  "node": "N3_CANDIDATE",
  "input_digest": "...",
  "upstream_artifact_hash": "...",
  "threshold_config_version": "g2.r7",
  "threshold_config_hash": "...",
  "route_budget": 250000,
  "precision": "fp32"
}
```

`N1/N2` 不消费门限，使用依赖版本 `"0.0"`；`N3/N4` 使用实际版本及门限内容哈希。所有节点均显式携带输入摘要、上游哈希、预算和精度。

对象 envelope 保存键组件、节点、产物和产物哈希。读取时同时校验：

- envelope 格式与键；
- 键组件完全相等；
- 产物哈希可复算；
- 诊断阶段再从零执行流水线比较产物。

## 6. 增量失效

失效起点：

| 变化 | 最早受影响节点 |
| --- | --- |
| 请求文本、种子、文本/图像修订、新增请求 | `N1_FEATURE` |
| 路由预算、精度档 | `N1_FEATURE`（键和上游产物均变化） |
| 配置代数、修订号、门限值、候选数 | `N3_CANDIDATE` |

对每个受影响请求：

- `start..4` 进入 `recomputed_nodes`；
- `1..start-1` 进入 `reused_nodes`；
- 四节点全部进入 `covered_nodes`。

`InvalidationPlan.validate()` 强制检查重算/复用不相交、并集等于覆盖集合、覆盖集合等于受影响请求的四节点笛卡尔集。

## 7. 资源裁决与并行调度

请求先按 ID 排序，再按批次大小切片；一个批次内按连续区间切分为最多 `workers` 个块。进程池 future 按提交顺序收集，批次结果按请求 ID 归并，因此完成时序不影响输出。

资源规划枚举：

- 不高于原配置的精度档；
- `1..requested_workers`；
- 固定候选批次边界集合。

可行组合按显式优先级排序。内存模型是确定性的计划上界：

```text
16 MiB 基础
+ 4 MiB * worker
+ 0.10 MiB * 批内请求数
```

该模型用于可复现裁决；实测浮点操作数在每批提交前再次检查。

## 8. 检查点与恢复

检查点包含：

- 单调递增 `checkpoint_version`；
- `schedule_key`（配置指纹 + 请求输入摘要）；
- 排序且唯一的已完成请求 ID；
- 每个已完成请求的稳定决策记录；
- 最后批次索引；
- 全内容 SHA-256。

每批所有 worker 成功后，才构造并原子替换检查点。恢复时：

1. 校验检查点结构和哈希；
2. 校验 schedule key；
3. 按原始批次边界识别已完整提交批次；
4. 跳过已提交批次，不调用路由 worker；
5. 丢弃所有未提交批次的内存中间状态。

若检查点把单个批次切成部分完成，返回 `E_CHECKPOINT_CORRUPT`。

缓存写入同样按批事务化：worker 读取已提交主缓存，把新对象写入 `uncommitted/` staging；只有检查点提交成功后才提升到主缓存。worker 失败、预算复检失败或检查点提交冲突都会清空 staging，避免重启后复用未提交状态。

## 9. 提交账本

`CommitController` 为配置账本和检查点提供共享 `StageLock`。锁通过 `fcntl.flock(LOCK_EX | LOCK_NB)` 获取，竞争时立即返回 `E_STAGE_BUSY`。

每次提交都是完整快照：

```json
{
  "kind": "config",
  "version": 2,
  "parent_version": 1,
  "requested_parent_version": 1,
  "drift_detected": false,
  "snapshot": {},
  "snapshot_checksum": "...",
  "content_checksum": "..."
}
```

若调用方期望的父版本或校验和已变化，则设置 `drift_detected=true`，仍以完整快照替换；若请求版本低于当前版本则返回 `E_SNAPSHOT_ROLLBACK`。

## 10. 诊断

诊断链路不信任缓存 envelope：

1. 用无缓存流水线重新生成每个节点的产物和哈希；
2. 从 `GENESIS` 开始逐节点重算上游哈希；
3. 重算键组件与键；
4. 比较缓存产物和独立复算产物；
5. 校验失效集合；
6. 对比恢复与全量重放的业务码和完整决策字节；
7. 对比两次路由重放字节。

首偏差通过 `first_request_id` 和 `first_node` 返回。报告控制码为 `OK` 或 `E_INCONSISTENT`。

