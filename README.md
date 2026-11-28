# 电子合同分片热点迁移模拟器

仅使用 Python 3 标准库的离线、确定性模拟工程。程序从固定种子生成合同文档、印章记录、签署事件、分片拓扑和访问请求，完成热点认定、容量校核、分批迁移、在线路由、原子切换、待回收标记和中断续迁，并输出可逐字节复现的结构化报告。

## 快速运行

```bash
# 默认场景，生成完整报告与中断快照
python3 -m contract_migration

# 等价入口
python3 main.py

# 指定报告和快照位置
python3 -m contract_migration \
  --output artifacts/contract_migration_report.json \
  --snapshot artifacts/interrupted_snapshot.json

# 独立从快照续迁并校验与全量迁移等价
python3 -m contract_migration \
  --resume artifacts/interrupted_snapshot.json \
  --output artifacts/resumed_contract_migration_report.json

# 参数注入异常，输出稳定整数错误码
python3 -m contract_migration --inject-error 1008

# 验收测试
python3 -m unittest discover -v
```

默认参数：`seed=20261128`、滑动窗口 `120`、迁移批次容量 `220000` 字节、容量迁移阈值 `8000 bp`（80%）。默认样本包含 240 份合同、5 个分片；热点真值为 `HT-000038..HT-000082`，跨越 `shard-a` 与 `shard-b` 的路由边界。

## 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--seed` | `20261128` | 确定性生成器种子 |
| `--window-size` | `120` | 热点滑动窗口请求数 |
| `--batch-capacity` | `220000` | 单迁移批次最大字节数 |
| `--migration-threshold-bp` | `8000` | 容量热点阈值，万分比 |
| `--interrupt-batch-index` | `1` | 内置崩溃测试的目标批次，0 起 |
| `--interrupt-after-contracts` | `3` | 崩溃前已双端同步的合同数 |
| `--interrupt-after-requests` | `20` | 崩溃前该批次已处理请求数 |
| `--snapshot` | `artifacts/interrupted_snapshot.json` | 中断快照输出；`-` 表示仅内存持久化 |
| `--output` | 依模式选择 | 报告路径；`-` 表示输出到 stdout |
| `--resume` | 无 | 从指定快照继续迁移 |
| `--inject-error` | 无 | 注入整数错误码并以退出码 2 返回 |
| `--compact` | 关闭 | stdout JSON 使用紧凑格式 |

## 默认场景结果

- 热点窗口为观察请求 `40..159`，两个热点分片各满足访问量与容量水位阈值。
- 热点范围 `HT-000038..HT-000082` 同属 `BATCH-HOT-07`，并按现有路由边界拆为两个归属切片，降低两个源分片负载。
- 目标分片按“容量足够时选择最低水位”规则选择，规划 4 个批次，迁出 45 份合同。
- 迁移中读请求由源分片服务；写请求先提交源分片，再同步目标分片，并逐次校验逻辑版本一致。
- 每个批次同步完成后原子替换区间路由，源端键转为 `pending_reclaim`，后续请求不再由源端服务。
- 内置中断发生在第二批中途；快照恢复后跳过已完成批次和已同步合同，最终逐合同归属、版本、键状态及整体状态签名均与全量迁移一致。
- 四个要求的人工边界样本和额外容量拒绝样本全部通过。

## 报告结构

报告使用固定插入键序写为 UTF-8 JSON，不带运行时间戳或随机键序。主要字段依次为：

- `run_parameters`：种子、窗口、批次容量和阈值。
- `execution_summary`：执行摘要。
- `field_sources`：契约文档、印章、事件、拓扑、访问请求等输入字段的来源路径。
- `topology`、`capacity_reconciliation`：分片声明及文档字节对账。
- `unified_contracts`：按合同号关联后的完整合同事件模型及逐字段来源路径。
- `initial_route_table`、`reference_router_validation`：初始区间路由和独立线性参考器校验。
- `hotspot_analysis`：逐分片访问计数、容量水位、热点评分、阈值依据、热点合同范围及跨边界拆分。
- `migration_plan`：目标候选、选择理由、待迁合同、字节数、批次和容量拒绝缺口。
- `access_routing`：逐请求路由、读写路径、源/目标可见版本和参考路由核对。
- `batch_switch_history`：每个批次切换前后的路由与负载。
- `route_diff`、`contract_shard_lifecycle`：最终路由差异及每份合同初始/最终归属。
- `load_distribution`、`source_reclaim_state`：最终容量分布与源端待回收状态。
- `hotspot_elimination`：按最终路由重新映射同一观察窗口后的热点消除判定。
- `interruption_resume`：快照摘要、续迁路由、逐合同归属及全量等价比较。
- `edge_cases`：人工边界样本、容量拒绝样本和整数错误码。
- `consistency_checks`：所有跨阶段验收结论。

## 关键语义

**确定性生成**

使用自实现的 64 位 `SplitMix64`，不依赖 `random` 模块、哈希随机化或当前时间。相同参数产生相同输入、报告和快照字节。

**区间路由**

合同号为固定宽度 `HT-000000` 格式，区间为闭区间。路由替换先构造完整的新区间列表，再一次性赋值；相邻同分片区间会规范合并。

**热点认定**

程序扫描全部候选滑窗，以“前两个分片请求数之和最大、同分时取较晚窗口”为选择规则。分片同时满足以下条件才认定为热点：

1. 窗口访问数不低于窗口大小的 20%。
2. 按初始路由归属合同文档字节计算的水位不低于迁移阈值。

热点评分为万分比整数：

```text
score_bp = (65 * access_share_bp + 35 * min(watermark_bp, 10000)) / 100
```

热点合同计数阈值由窗口内单合同最大访问数的一半向上取整得到，再取访问密集的连续合同簇。

**迁移与容量**

每个热点切片按文档字节顺序贪心分批，单批不超过 `--batch-capacity`。目标分片必须在承接后不超过容量上限，并在合格候选中选择当前水位最低者；无合格目标时报告 `gap_bytes` 和错误码 `1012`。

**在线读写**

`migrating` 状态的合同仍由源分片服务读请求。写请求执行 `source_commit` 后立即执行 `target_sync`，两条记录均使用同一个递增逻辑版本。路由切换后源副本为 `pending_reclaim`，不再参与服务。

**快照续迁**

快照包含规划指纹、已完成批次、每批已同步合同、全部键状态与版本、当前路由、请求游标和下一批次位置。恢复时校验规划指纹与状态一致性；已切换批次不重放，当前批次已同步合同不重复复制，再继续未完成请求和后续批次。

**等价验收**

续迁结果与全量迁移逐项比较：最终区间路由、240 份合同的归属、每份合同的逻辑版本、所有物理副本状态及整体 SHA-256 状态签名必须一致。

## 整数错误码

| 整数码 | 符号 | 含义 |
| --- | --- | --- |
| `1001` | `INVALID_SEED` | 种子不是非负整数 |
| `1002` | `INVALID_WINDOW_SIZE` | 窗口不是正整数 |
| `1003` | `INVALID_BATCH_CAPACITY` | 批次容量不是正整数 |
| `1004` | `INVALID_THRESHOLD` | 迁移阈值不在 `[0,10000]` |
| `1005` | `UNKNOWN_CONTRACT` | 未知合同号 |
| `1006` | `ROUTE_GAP` | 路由区间存在缺口 |
| `1007` | `OVERLAPPING_ROUTE` | 路由区间重叠或替换非法 |
| `1008` | `CAPACITY_EXCEEDED` | 容量超限 |
| `1009` | `INVALID_REQUEST_OP` | 非法访问操作 |
| `1010` | `SNAPSHOT_CORRUPT` | 快照损坏或状态不一致 |
| `1011` | `RESUME_PLAN_MISMATCH` | 快照与重生成迁移计划不匹配 |
| `1012` | `TARGET_CAPACITY_INSUFFICIENT` | 无目标分片可承接，报告缺口 |

## 代码结构

```text
contract_migration/
  generator.py   固定种子输入生成
  router.py      区间路由、原子替换、独立参考路由
  hotspot.py     滑窗热点与容量水位分析
  planner.py     目标选择、容量校核、批次规划
  simulation.py  在线路由、双写同步、切换、快照恢复
  edges.py       人工边界样本与错误码
  workflow.py    端到端编排与稳定报告
  cli.py         命令行入口
tests/
  test_contract_migration.py
```
