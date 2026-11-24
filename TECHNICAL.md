# 技术说明

## 1. 处理流水线

```text
JSONL/JSON/SQLite
      |
      v
严格解析 + 逐记录拒绝
      |
      v
v1 秒量化 -> 绝对毫秒
v2 分片 <-> 元数据交叉校验
      |
      v
统一规则目录 (RuleCatalog)
  - 权威时间轴
  - 墓碑覆盖
  - 跨代冲突
  - 依赖闭包
      |
      v
告警时间定版
  - 正常绑定 / 未生效 / 墓碑禁用
  - E_VERSION_AMBIGUOUS
      |
      v
主机级并行归并
  - 谱系图与环检测
  - 固定路径簇
  - float64 上界参考簇
  - E_WINDOW_AMBIGUOUS
      |
      v
补丁 SQLite 事务 -> 变化规则键 -> 依赖闭包 -> 局部重算
      |
      v
最终全量重算逐字段比对 -> 原子报告/检查点
```

## 2. 时间与窗口口径

### v1

`effective_after_s` 和 `dedup_window_s` 都是非负整数。实现把它们视为源精度的
向下量化值：

```text
absolute_effective_ms = observation_clock_ms + effective_after_s * 1000
window_ms             = dedup_window_s * 1000
effective_error_ms    = 999
window_error_ms       = 999
```

这样 v1 版本的真实生效时刻位于
`[effective_ms, effective_ms + 999]`，窗口真实长度位于
`[window_ms, window_ms + 999]`。该区间只用于定版重叠和窗口精度诊断，不用于
篡改名义绑定时刻。

### v2/补丁

`effective_ms` 与 `dedup_window_ms` 直接使用绝对整数毫秒，量化误差为 0。

系统当前时间未参与任何规则时间换算。`perf_counter()` 只用于资源预算和报告耗时。

## 3. 权威规则裁决

对每个规范规则键：

1. 收集 v1、v2、已接受补丁的所有版本。
2. 同一 `effective_ms` 内按以下稳定秩取赢家：

```text
(generation, tombstone, source_rank, logical_ts, batch_id, record_index)
source_rank: patch > v2 > v1
```

3. 任意告警时刻取不晚于该时刻的最大生效组赢家。
4. 如果赢家是墓碑或 `enabled=false`，告警绑定状态为 `inactive`，不进入事件簇。

因此较晚生效的墓碑天然覆盖较早非墓碑记录；同一时刻则由更高代数墓碑胜出。

### 定版歧义

分别取告警时刻前最新的 v1 版本和最新绝对时间版本。若两者生效起点差不超过量化
误差界，且告警时刻落在其量化区间交叠段内，则返回
`E_VERSION_AMBIGUOUS`。诊断列出两个候选的规范键、源规则键、来源、代数、生效
时刻和误差界。该告警不参与归并，其他告警继续处理。

## 4. 事件簇

归并键：

```text
merge_key = SHA256(canonical_rule_key || 0x00 || normalized_payload_digest)
```

成员必须同时满足：

1. 同一 `host_id`；
2. 同一 `merge_key`；
3. 与当前簇代表的时间差不超过新告警绑定版本的 `dedup_window_ms`；
4. 新告警进程与簇内任一进程在同一谱系连通分量。

告警按 `(alert_ts_ms, alert_id)` 稳定排序。若一个告警同时可连多个已有候选簇，
则稳定合并这些候选簇。最终簇 ID：

```text
SHA256(host_id || 0x00 || merge_key || 0x00 || representative_alert_id
       || 0x00 || sorted_member_alert_ids)
```

因此簇 ID 不只取决于代表，也取决于完整成员集；窗口参考路径新增成员时不会被
误认为与固定路径完全相同。

### 进程图

- 有向边：`process_guid -> parent_process_guid`。
- 环检测：按标识排序的迭代 DFS，环旋转到字典序最小的起点。
- 谱系可达性：父子无向图的并查集连通分量。这样父、子、兄弟及共同祖先路径均可
  归入同一谱系。

## 5. 窗口精度双路径

固定路径：

```text
delta_ms <= dedup_window_ms
```

float64 参考路径：

```text
delta_seconds = alert_ts_ms / 1000.0 - representative_ts_ms / 1000.0
limit_seconds = (dedup_window_ms + window_error_ms) / 1000.0
```

只有 v1 窗口有非零 `window_error_ms`。若两条路径对某个非代表告警的成员关系不同，
固定路径保持分离，并输出：

- `fixed_cluster_id`
- `float_cluster_id`
- `residual_ms = delta_ms - dedup_window_ms`
- `residual_seconds`
- `accumulated_error_bound_ms`

每个簇同时输出最小边界余量 `min_boundary_margin_ms`、累计余量
`boundary_margin_total_ms`、判定次数 `boundary_checks` 和累计误差界
`accumulated_error_bound_ms`。

## 6. 补丁账本与增量重算

SQLite 表：

- `baseline_versions`：v1/v2 基线版本。
- `patch_batches`：批次水位、到达序号、原始 payload 与哈希。
- `patch_rules`：逐规则审计行。
- `engine_meta`：观测时钟、输入指纹、补丁拒绝审计。
- `checkpoints`：检查点水位、结构版本、簇摘要和快照校验和。

每个批次：

1. 获取工作区文件锁；
2. `BEGIN IMMEDIATE`；
3. 检查批次 ID 唯一性；
4. 将候选批次与现有账本组成候选规则目录；
5. 复验绝对时间规则的代数单调性；
6. 任何新代数问题都抛 `E_PATCH_CHAIN`；
7. 插入批次和规则审计行；
8. 提交；任何异常回滚。

批次成功后：

```text
旧目录 diff 新目录 -> changed_keys
changed_keys 沿 parent_rule_keys 扩展 -> closure
closure 内告警重定版
closure 内旧簇删除，受影响主机局部重归并
closure 外簇、进程环、不可达诊断保留
```

每批可写一次检查点。最终再从最终规则目录做全量计算并比较：

- 规则视图
- 告警绑定
- 簇划分
- 版本歧义
- 窗口歧义
- 进程环
- 拒绝清单

比较不一致时报告 `E_RECOMPUTE_MISMATCH`，并使用全量结果作为最终报告状态。

## 7. 并发与预算

调度器以 `host_id` 排序后的任务为输入：

- 确定性准入容量：`max_workers + pending_host_queue_limit`；
- 容量内使用有界队列，容量外稳定返回 `E_QUEUE_FULL`；
- 工作线程从队列消费，完成顺序不影响结果汇总；
- 每主机启动时记录 `perf_counter()` 截止时间；
- 归并内每次处理告警前检查截止时间；
- `E_TIMEOUT` 丢弃该主机本轮全部簇；
- 告警数超过 `max_alerts_per_host` 时返回 `E_BUDGET`；
- 增量重算不能绕过全局准入集合，避免不同调用顺序带来预算差异。

## 8. 检查点与原子性

检查点快照包含：

- `structure_version`
- `merge_semantics_version`
- 输入指纹
- 已应用批次 ID 集合
- 逻辑水位
- 规则目录摘要
- 簇摘要
- 完整可恢复计算状态
- 状态与簇摘要的自校验值

保存顺序：

1. 获取 `.engine.lock`；
2. 在 `tmp/` 写临时 JSON；
3. 重新解析并校验；
4. 计算文件 SHA-256；
5. 原子替换快照；
6. 在同一锁内提交 SQLite 检查点行。

若在 3-5 之间被杀，SQLite 无新行，旧检查点仍有效；若在 5-6 之间被杀，快照成为
无引用孤立文件，恢复时忽略。恢复只使用能通过文件哈希、状态摘要、簇摘要、结构
版本和输入指纹校验的最高序号检查点。损坏的最新项会进入残差清单并回退。

报告写入共享同一文件锁；并发写入冲突稳定返回 `E_LOCK`。

## 9. 业务码

| 业务码 | 含义 |
|---|---|
| `E_JSON` / `E_SCHEMA` | JSON 或结构无法解析 |
| `E_ALERT_ID_DUP` | 告警 ID 重复 |
| `E_ALERT_TS` | 告警时间非法 |
| `E_SEVERITY` | severity 超出声明区间 |
| `E_DIGEST` | payload digest 非 64 位十六进制 |
| `E_RULE_KEY` / `E_RULE_UNKNOWN` | 规则键非法或无法解析 |
| `E_V1_TIME` / `E_V1_WINDOW` | v1 时间/窗口非法 |
| `E_V2_TIME` / `E_V2_WINDOW` / `E_V2_GENERATION` | v2 字段非法 |
| `E_V2_GENERATION_ORDER` | v2 代数链非单调 |
| `E_V2_META_MISMATCH` / `E_V2_META_MISSING` | 分片与元数据不一致 |
| `E_PATCH_DUP` | 补丁批次重复 |
| `E_PATCH_CHAIN` | 补丁破坏代数单调性 |
| `E_RULE_CONFLICT` | 两代规则字段结论冲突 |
| `E_VERSION_AMBIGUOUS` | 告警落在两代生效重叠带 |
| `E_RULE_NOT_EFFECTIVE` / `E_RULE_INACTIVE` | 告警尚无生效规则/规则被禁用 |
| `E_PROC_CYCLE` | 进程父子图存在环 |
| `E_WINDOW_AMBIGUOUS` | 定点与 float64 窗口判断不同 |
| `E_RECOMPUTE_MISMATCH` | 增量与全量结果不一致 |
| `E_TIMEOUT` | 单主机墙钟超时 |
| `E_QUEUE_FULL` | 主机超出确定性准入窗口 |
| `E_BUDGET` | 单主机告警数超限 |
| `E_LOCK` | 工作区文件锁冲突 |
| `E_CHECKPOINT_CORRUPT` / `E_CHECKPOINT_REBUILD` | 检查点损坏或整体重建 |

## 10. 已知边界

- 强制终止进程时不执行退出清理，也不承诺写完报告；`SIGKILL` 语义不在可捕获范围。
- `perf_counter` 耗时和调度完成时序天然受机器负载影响，因此报告中的耗时字段不
  参与“结论一致”比较。
- 补丁必须携带可独立解析的完整版本字段；设计不承诺跨批次部分字段补丁解析。
- 本次容器为 Linux/WSL2，Windows 锁分支使用 `msvcrt.locking`，但未在本容器中
  实际执行 Windows 分支。
