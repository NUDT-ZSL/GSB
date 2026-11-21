# 技术说明：特征漂移监控本地应用

## 1. 设计总览

应用只依赖 Python 标准库与 NumPy，全部输入由固定种子在本地确定性生成，
无任何网络访问、账号或在线服务。模块按数据流单向分层：

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 错误治理 | `drift_monitor/errors.py` | 稳定业务码与结构化上下文 |
| 确定性 | `drift_monitor/hashing.py` | 规范化 JSON 与 SHA-256 内容哈希 |
| 配置 | `config_model.py` / `config_reader.py` / `config_migration.py` | v1/v2/v3 读取、双向迁移、往返校验 |
| 数据 | `drift_monitor/data.py` | 固定种子批次流、四类漂移注入、入批校验 |
| 统计 | `drift_monitor/statistics.py` | 数值/类别双口径统计、合并与精确扣除 |
| 窗口 | `drift_monitor/windowing.py` | 参考窗口滑动、出窗贡献精确移除、重算校验 |
| 检验 | `drift_monitor/drift.py` | 均值/分位数/占比距离、严重度、平局排序 |
| 缓存 | `drift_monitor/cache.py` | 内容寻址、缓存键校验、按键重放 |
| 治理 | `snapshot.py` / `resources.py` | 版本化快照、过期告警、资源上限/降级 |
| 编排 | `drift_monitor/engine.py` | 缓存复用、失效传播、全量重算对照 |
| 入口 | `cli.py` / `app.py` | JSON 命令行与本地交互控制台 |

## 2. 三种配置格式

* v1：无字段名的竖线位置分列行，固定列序
  `name|type|threshold|window|enabled`。
* v2：`key=value` 竖线分隔行，可显式书写 `methods`、`quantiles`、
  `min_samples`。
* v3：结构化 JSON，携带检验方式链、分位数口径与最小样本数。

读取按格式头（v1/v2）或 JSON 字段集（v3）分派。结构问题统一为
`E_FORMAT_CORRUPT` 并带 1 基行号与字段名；未知版本为
`E_VERSION_UNSUPPORTED`；语义问题（未知检验方式、阈值越界）在
`MonitorConfig.validate()` 中归为 `E_CONFIG_INVALID`。

### 迁移与往返

* v1→v2/v3 会补全检验方式、分位数口径、最小样本数，并在 `provenance`
  中逐项记录补全来源常量。
* v3→v1 对无法表达的能力按显式 `freeze` 或 `drop` 策略处理，输出
  `discarded` 明细。
* `assert_roundtrip` 逐特征比较阈值、检验方式、参考窗口长度等，首个差异
  即抛 `E_ROUNDTRIP_MISMATCH` 并指出特征与字段。

## 3. 确定性与统计口径

* 每个批次的随机数发生器由“主种子 + 批次号 + 流标签”派生，批次可独立
  复现，与生成顺序无关。
* 所有哈希都基于排序键、固定浮点格式的规范化 JSON，禁止依赖字典遍历
  顺序或 `str(dict)`。
* 数值列维护计数、和、平方和与有序值多重集合；类别列维护级别频次、
  缺失数与级别宇宙，两类口径在代码与报告中显式区分。
* 求和统一使用 `math.fsum` 对有序值集合求和，使“增量扣除路径”与
  “按批次明细独立重算路径”得到逐字节相同的浮点结果。

## 4. 滑窗精确移除

`ReferenceWindow.append_and_slide` 同时维护两条路径：

1. 增量路径：对出窗批次做充分统计量代数扣除（数值使用有序多重集合归并
   删除，类别按频次逐项扣减），再合并入窗批次。
2. 校验路径：仅使用仍在窗口内的批次明细，从空开始 `fold_window` 重算。

两条路径逐字段比对，不一致即 `E_INCONSISTENT`。报告同时区分
`window_*`（窗口内）与 `cumulative_summary`（全量累计），并记录窗口起止
批次、出窗批次与移除量。

## 5. 内容寻址缓存与失效传播

缓存键由六类成分决定：窗口样本内容哈希、检验方式、阈值、统计口径版本、
特征列定义摘要、上游产物哈希。键相同复用快照，键不同重算；不可解析的
键返回 `E_CACHE_KEY_INVALID`，并提供 `replay-cache` 入口按键重放。

配置变更（口径调整、启用/禁用、列类型重定义）经 `_impact_analysis`
计算受影响特征与批次后缀，输出互斥的重算集合与复用集合；集合出现交集即
`E_INCONSISTENT`。`rebuild_from_scratch` 用全新缓存做全量重算，用于证明
局部修改结果与全量结果逐字节一致。

## 6. 告警治理与资源上限

* 配置快照与告警均带单调版本号与内容校验和；内容变化整体替换并递增
  版本，回退旧版本返回 `E_SNAPSHOT_ROLLBACK`。
* 告警记录缓存键、窗口范围、配置版本与校验和；依赖版本变化而仍引用旧键
  时，`assert_fresh` 返回 `E_ALERT_STALE`。
* `ResourceLimits` 控制单批样本数、缓存条目数与驻留统计量内存估算；
  硬上限抛 `E_RESOURCE_LIMIT`，驻留内存可配置为降级并记录降级类别。

## 7. 业务错误码对照表

| 代码 | 触发场景 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 未知/缺失配置版本 |
| `E_FORMAT_CORRUPT` | 头截断、竖线列数不符、键缺失、阈值不可解析 |
| `E_ROUNDTRIP_MISMATCH` | 往返后首个特征字段不一致 |
| `E_CONFIG_INVALID` | 未知检验方式、阈值/窗口/分位数越界 |
| `E_TYPE_UNSUPPORTED` | 不支持的列类型 |
| `E_EMPTY_BATCH` | 批次样本数为零 |
| `E_ILLEGAL_VALUE` | 非法标记、类型不符、NaN/Inf |
| `E_WINDOW_INVALID` | 窗口长度非正或步长大于窗口长度 |
| `E_CACHE_KEY_INVALID` | 缓存键不可解析、校验失败或不存在 |
| `E_INCONSISTENT` | 重算/复用交集、窗口双路径不一致 |
| `E_SNAPSHOT_ROLLBACK` | 旧快照版本试图覆盖新版本 |
| `E_ALERT_STALE` | 告警依赖的配置/窗口/缓存键已变化 |
| `E_RESOURCE_LIMIT` | 样本数、缓存条目或驻留内存超限 |
