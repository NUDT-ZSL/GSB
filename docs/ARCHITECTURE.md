# 本地诊断工作台技术说明

面向 LLM 检索增强与工具调用链路，围绕**工具结果格式版本演进下的一致性
重组、增量失效回溯与快照整体替换**构建。运行时仅依赖 Python 标准库；
NumPy 只用于确定性样本生成。全程离线，无外部网络、账号或容器。

## 1. 目录与模块职责

| 模块 | 职责 |
| --- | --- |
| `diagwb/errors.py` | 稳定业务错误码与带上下文的 `DiagError` |
| `diagwb/hashing.py` | 规范化 JSON、SHA-256 内容哈希、缓存键组合 |
| `diagwb/models.py` | 统一内部表示：结果节点、分片引用、来源链、片段、DAG 节点 |
| `diagwb/formats.py` | v1/v2/v3 读取器与写入器、版本分派、损坏定位 |
| `diagwb/migration.py` | 双向迁移、升级补全审计、降级丢弃审计、逆向信封 |
| `diagwb/equivalence.py` | 跨格式语义等价裁决与往返校验 |
| `diagwb/cache.py` | 内容寻址缓存（解析产物 / 片段产物）与完整性校验 |
| `diagwb/store.py` | 版本化结果仓库、内容变更检测、增量失效集合 |
| `diagwb/graph.py` | 组装 DAG、调用对齐、片段合并、答案重算、陈旧拒绝 |
| `diagwb/lineage.py` | 逐片段来源重放与断链定位 |
| `diagwb/snapshots.py` | 三部件版本化快照、校验和、回退/损坏/并发写控制 |
| `diagwb/limits.py` | 资源上限与降级类别记录 |
| `diagwb/workbench.py` | 编排门面、跨模块自检、HTML/JSON 诊断视图 |
| `diagwb/samples.py` | 固定种子（`SAMPLE_SEED=20240521`）的 NumPy 样本生成 |
| `diagwb/cli.py` | 本地交互式工作台 |

## 2. 三种磁盘格式

- **v1**：无字段名竖线分列 `tool_id|payload|status`，无结构信息与来源标记。
- **v2**：带字段名 `key=value` 竖线行，含 `result_schema_version` 与置信区间。
- **v3**：结构化 JSON，含分片引用、多结果聚合声明与来源链。

字段值统一 URL 编码，`|` 与 `=` 永不歧义。v1/v2 文件以
`# FORMAT_VERSION=N` 头分派，v3 可读头或裸 JSON；无头时按字段集推断。
未知版本抛 `E_FORMAT_VERSION_UNSUPPORTED`；字段数不符、必填缺失、payload
不可解析、状态码越界抛 `E_FORMAT_CORRUPT`，上下文带 `line_no` 与
`field_offset`。受控状态集合：`OK/EMPTY/ERROR/THROTTLED`。

## 3. 规范化、升级补全与降级丢弃

任意格式先规范化为 `ResultNode`（含 `ShardRef` 与 `LineageNode`）。

- **升级补全**：v1/v2 缺失的 schema 版本、置信区间、来源链、分片与聚合被
  补全，每个字段写一条 `CompletionRecord(field, action, source, reason)`，
  置信区间由原始内容哈希与状态确定性派生，可重复复算。
- **降级丢弃**：v3 → v1/v2 依据显式 `MigrationPolicy`（`shards`、
  `aggregation`、`lineage` 取 `flatten/drop/keep`）处理，每个不可原生表达
  的项写一条 `DiscardRecord`，丢弃项绝不静默消失。
- **逆向信封**：`flatten/keep` 路径在 payload 内嵌 `_reconstruction`
  信封，升级时据此还原分片、聚合与来源链，实现无损往返；`drop` 路径不写
  信封，损失真实不可逆，等价裁决据此报 `E_SEMANTIC_DRIFT`。

## 4. 语义等价与往返

- 硬核心 = 业务 payload（剔除内部信封键）+ 状态 + 分片归属，必须一致。
- v1 天然不具备的能力（置信区间、来源链、分片来源等）记为**能力缺口**，
  原生三格式仍判等价；只有经历降级且结构真实无法还原时才判漂移，并列出
  不可还原字段。
- 往返校验逐项比对 payload、状态、分片归属；v2/v3 额外在声明精度
  （`CI_PRECISION=1e-6`）内比对置信区间与来源链。

## 5. 组装 DAG、合并与答案重算

阶段固定为：`load → migrate → parse → align → merge → assemble`，
边在构建时即为有向无环图，`detect_cycle` 做 DFS 环检测。每条片段记录
来源 `tool_id/result_ref/result_format_version`、合并位置、区间、得分与
所用缓存键。合并规则：

- 同一结果不得同时进入答案与失败集；
- 片段来源必须存在于调用集合；
- 同一结果同一区间重复合并抛 `E_MERGE_CONFLICT`；
- 同分片段按 `tool_id` 字典序打破平局；
- 答案可仅由逐片段明细经 `recompose_answer` 独立重算且哈希一致。

## 6. 内容寻址缓存与增量失效

缓存以 SHA-256 落盘。解析键与片段键均纳入：结果原始内容哈希、格式版本、
结构声明摘要、调用参数摘要，片段键额外纳入上游解析产物哈希。产物写入时
再算产品哈希，读取时复核，篡改即按未命中处理。

仓库条目以单调 `entry_version` 留存历史版本。只有**内容哈希或格式版本
实际变化**才判受影响；二者均未变的条目不判受影响、缓存复用。失效报告输出
受影响/未受影响结果、受影响片段、复用与重算集合，二者无交集且并集覆盖
受影响集合。

## 7. 过期拒绝与来源重放

片段携带记录时的 `entry_version`。组装期若命中的片段来自被取代版本，删除
旧缓存并标记 `E_RESULT_STALE` 后重算，绝不服务陈旧数据；显式重放旧片段时
直接抛 `E_RESULT_STALE`，指出 `result_ref` 与来源/当前版本。重放沿
`store → parse → fragment → merge` 逐跳核对，缺缓存、结果消失、文本/区间/
位置/路径不符均抛 `E_LINEAGE_BROKEN` 并指出断点节点。

## 8. 快照整体替换与并发写控制

仓库、格式定义、答案三个部件各带单调版本号与内容校验和。任何变化都整体
发布新版本文件（临时文件 + `os.replace` 原子切换），不做增量修补。版本
只增不减，回退抛 `E_SNAPSHOT_ROLLBACK`；校验和不符或文件损坏抛
`E_SNAPSHOT_CORRUPT` 并指明部件与版本号。提交经 `O_CREAT|O_EXCL` 非阻塞
阶段锁保护，并发冲突的后到者立即得到 `E_STAGE_BUSY`，不等待。

## 9. 资源上限、降级与自检

`ResourceLimits` 提供驻留结果数、缓存条目总量、单结果分片数三项可配置
上限，超限抛 `E_RESOURCE_LIMIT`；工作台另提供优雅降级入口
`enforce_or_degrade_results`，裁剪驻留集并在 `DegradationLog` 标记降级
类别。自检覆盖：图无环、缓存键与产品哈希可互校、重算/复用无交集且覆盖
受影响集合、答案可独立重算一致。

## 10. 可视化

- 交互式 CLI：`python3 -m diagwb.cli .`
- 静态报告：`reports/diagnostic_report.html` 与 `.json`，含拓扑与命中/
  重算状态、失效传播、迁移补全/丢弃、逐片段来源路径、快照版本与最近错误码。
- 工作台 `format_view` 支持在 v1/v2/v3 视图间切换同一结果。
