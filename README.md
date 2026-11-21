# DriftMon

面向表格与流式特征数据的本地特征漂移监控应用。项目从零生成确定性批次流，提供窗口统计、逐特征漂移检验、内容寻址缓存、增量依赖失效、版本化告警、旧告警追溯、资源限制、并发原子写和本地交互仪表盘。

不依赖 Docker、外部数据集或网络服务。默认数据由固定种子在本地生成，所有监控工件均使用规范化 JSON 与 SHA-256。

## 快速开始

已准备好的本地环境：

- Python 3.11.18
- NumPy 1.26.4
- SciPy 1.13.1
- Flask 3.0.3
- pytest 8.3.3
- 虚拟环境：`.venv/`

从零安装：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

运行完整确定性演示：

```bash
.venv/bin/python -m driftmon demo
```

演示会清空 `runtime/demo`，依次执行初始构建、注入漂移、生成并追溯告警、滑动窗口、窗口长度调整、统计口径调整、特征禁用、列类型重定义和一致性自检，并把结果写入 `artifacts/demo_report.json`。

运行测试：

```bash
.venv/bin/python -m pytest -q
```

启动本地交互应用：

```bash
.venv/bin/python -m driftmon serve --root runtime/app --host 127.0.0.1 --port 8080
```

浏览器访问 `http://127.0.0.1:8080`。

## 项目结构

```text
driftmon/
  data.py         确定性批次生成、校验、批次指纹
  stats.py        数值/类别统计、批块累加器、快照组装
  detectors.py    漂移量、阈值、严重度、平局排序
  cache.py        内容寻址缓存、原子发布、并发锁、键查询
  engine.py       多级流水、增量失效、复用/重算报告
  alerts.py       告警版本、校验和、原子整体替换
  lineage.py      按历史缓存键重放告警
  selfcheck.py    跨模块一致性检查
  service.py      持久化应用状态、操作与错误记录
  web.py          Flask API 与仪表盘
  cli.py          demo / serve / selfcheck / trace 等命令
tests/            32 项确定性、增量、追溯、资源与并发测试
artifacts/        演示与验证记录
```

## 确定性设计

- 每个批次、每个特征列、每个随机阶段都从
  `sha256(seed|batch_index|feature_name|phase)` 派生独立 PCG64 流。
- 列之间不共享随机流；某列重定义不会改变其他列的原始值。
- 支持在指定批次注入均值平移、方差放大、类别占比变化和缺失率上升。
- 所有落盘内容通过 `canonical_bytes()` 生成：键排序、JSON 紧凑分隔符、浮点保留 12 位并规范化负零。
- 统计量、批次指纹、检验结果、缓存条目、告警内容均不依赖系统时间或字典插入顺序。
- 数值统计使用 `math.fsum` 和显式线性分位数插值，不依赖隐式汇总顺序。

默认配置：

```text
seed=20261121, batches=8, batch_size=64,
window_size=3, slide_step=1,
numeric_test=mean_shift, numeric_threshold=0.8,
category_threshold=0.20
```

默认特征：`channel`、`region`（类别），`device_score`、`session_minutes`、`transaction_amount`（数值）。

## 批次复核与统计口径

`generate_batch()` 复核样本数、数值有限性、类别水平和缺失值，并输出批次审计与指纹。每个特征/批次产物同时保存四套统计：

| 口径 | 范围 | 用途 |
| --- | --- | --- |
| `reference` | 当前批之前的 `window_size` 批 | 漂移检验参考 |
| `current` | 当前批 | 漂移检验当前样本 |
| `monitor_window` | 包含当前批的 `window_size` 批 | 滑窗统计视图 |
| `full` | 从第 0 批到当前批 | 全量累计视图 |

若当前批为 `b`，参考窗口为 `[max(0, b-W), b-1]`，监控窗口为 `[max(0, b-W+1), b]`。报告中同时记录窗口起止批次、移出批次、移出样本数和全量样本数。

数值列统计包含有效数、缺失数、均值、总体方差、标准差、最小/最大值、总和和配置分位数。类别列只包含数量、占比、众数等类别口径，不生成均值和方差。

`FeatureAccumulator` 以完整批次块累积。移除窗口外批次时直接删除该批次原始块，再从剩余块重算统计量，不使用“减均值/减平方和”的近似更新。

## 漂移检验与严重度

数值列支持两种配置：

- `mean_shift`：
  `abs(current_mean - reference_mean) / max(reference_std, 1e-12)`
- `quantile_distance`：
  `mean(abs(current_q - reference_q) / max(reference_std, 1e-12))`

类别列使用总变差距离：

`0.5 * sum(abs(current_proportion - reference_proportion))`

检验结果保存完整输入明细、公式、漂移量、阈值、是否命中、严重度、统计量内容哈希。任意检验可由统计量工件独立重算并逐字段一致。

严重度按 `drift / threshold` 分级：

```text
drift < threshold       NONE
1.0 <= ratio < 1.5      LOW
1.5 <= ratio < 2.5      MEDIUM
ratio >= 2.5            HIGH
```

当前批次全部特征按 `(-drift, feature_name)` 排序，漂移量相同时按特征名升序打破平局。禁用特征输出 `DISABLED`，无可比样本输出 `INSUFFICIENT_REFERENCE`。

## 内容寻址缓存

每个特征/批次拆成两个缓存工件：

1. `feature_stats`
2. `feature_test`

统计键组成包含：

- 参考窗口样本内容哈希
- 监控窗口与全量历史哈希
- 当前列内容哈希
- 数值检验方式或类别占比距离
- 数值/类别阈值与分位数配置
- 统计口径版本 `stat_version`
- 特征列定义摘要
- 上游特征列/历史产物哈希

检验键额外包含统计工件键和统计工件内容哈希，作为直接上游产物哈希。

缓存文件同时保存 `key_components`、`content`、`content_sha256` 和 `entry_checksum`。可通过以下命令查看任一键的组成：

```bash
.venv/bin/python -m driftmon cache-explain <cache-key> --root runtime/app
```

写入流程使用独占锁、临时文件、`fsync` 和 `os.replace` 原子发布。同一键的竞争任务只有一个获得写权，其余等待后读取完整文件；线程和跨进程竞争均有测试覆盖。

## 增量失效规则

引擎比较前后配置和游标，计算精确的受影响特征/批次集合：

| 变更 | 受影响范围 |
| --- | --- |
| 参考窗口前进 | 新批次的所有特征产物 |
| 窗口长度变化 | 参考窗口或监控窗口范围发生变化的批次后缀 |
| 单特征统计口径版本变化 | 该特征全部活动批次 |
| 单特征禁用/启用 | 该特征全部活动批次 |
| 列重定义为另一类型 | 该列全部活动批次；其他列复用 |
| 漂移事件配置变化 | 该特征从最早变化批次开始的后缀 |
| 全局种子/批大小变化 | 全部活动产物 |

一次运行会输出：

- `affected_products`
- `recomputed_products`
- `reused_products`
- `removed_products`
- `affected_features` 与 `affected_batches`
- 每个特征的受影响后缀
- 覆盖率和交集校验

重新启用某特征时，如果旧启用状态工件仍在缓存中，可直接复用旧快照而不重写。每次增量结果都会与相同配置下的全量强制重算比较，规范化快照逐字节一致。

## 告警与追溯

告警内容包含：

- 单调递增 `version` 与 `alert_id`
- 内容 SHA-256 `checksum`
- 配置版本
- 当前批、参考窗口、监控窗口、全量范围
- 每个命中特征的漂移量、阈值、严重度
- 统计键、检验键和两个内容哈希

告警写入采用“历史版本文件不可变 + `current.json` 原子整体替换”。如果检测到当前告警校验和异常，下一次发布从历史最大版本继续递增并整体替换当前文件，而不是补丁式修复。

追溯会按告警记录逐项加载统计与检验缓存，校验内容哈希，重新执行检验并重建告警内容与校验和。任一项缺失或不一致返回：

```text
E_LINEAGE_BROKEN
missing: ["cache:stats:<key>", ...]
```

## 资源限制与错误码

`ResourceLimits` 可配置：

- `max_batch_samples`
- `max_cache_entries`
- `max_stats_memory_bytes`
- `max_parallel_features`
- `resource_policy`: `strict` 或 `degrade`

严格模式超限返回 `E_RESOURCE_LIMIT` 并指出字段。降级模式支持：

- `CACHE_ENTRY_LIMIT`：不落盘新条目，但返回完整计算结果并标记降级
- `PARALLELISM_CAPPED`：将并行特征数降到上限并标记降级

主要错误码：`E_CONFIG_INVALID`、`E_RESOURCE_LIMIT`、`E_LINEAGE_BROKEN`、`E_CACHE_CORRUPT`、`E_CHECKSUM_MISMATCH`、`E_BATCH_INVALID`、`E_STATE_INVALID`、`E_LOCK_TIMEOUT`。

## 交互应用

仪表盘展示：

- 当前批次、参考/监控窗口、移出样本数、全量累计
- 逐特征漂移量、阈值、严重度、检验方式和缓存命中/重算状态
- 受影响、重算、复用集合及覆盖率
- 告警版本、校验和与一键追溯
- 最近业务错误码
- 六项跨模块自检结果

演示按钮支持注入漂移、滑动一批、窗口改为 2 批、统计口径调整、禁用/启用特征、列类型重定义、清空缓存从零构建和运行自检。

API：

```text
GET  /api/state
POST /api/reset
POST /api/advance
POST /api/window
POST /api/demo/inject
POST /api/stat-version/<feature>
POST /api/features/<feature>/toggle
POST /api/features/<feature>/redefine
POST /api/selfcheck
GET  /api/cache/<key>
GET  /api/alerts/<alert_id>/trace
```

## 实际验证结果

环境：Python 3.11.18、NumPy 1.26.4、SciPy 1.13.1、Flask 3.0.3、pytest 8.3.3。

自动化测试：

```text
32 passed
```

覆盖：

- 同批次/同配置统计量与指纹逐字节一致
- 数值均值/分位数检验与类别占比距离可独立复算
- 同漂移量按特征名升序
- 窗口统计与直接切片一致，移出批次后精确重算
- `window_size`/`slide_step` 错误字段
- 四类增量失效及全量重建逐字节一致
- 缓存键组成可查询
- 告警版本、校验和、篡改检测、整体替换与历史重放
- 缺失缓存项返回 `E_LINEAGE_BROKEN` 和精确缺失路径
- 单批、缓存、内存、并行资源上限与降级
- 24 线程与 16 任务/8 进程同键并发写，以及多进程全局条目上限
- Flask 页面、状态 API、错误码 API 与缓存键 API

确定性演示的已验证关键结果：

- 初始 `b0-b2`：10 个特征产物全量重算，0 复用。
- 第 3 批注入金额均值漂移：仅重算 5 个新批次产物，复用 10 个旧产物；生成 v1 告警并追溯成功。
- 滑动到第 4 批：重算 5 个新产物，复用 15 个旧产物。
- 窗口改为 2：受影响批次后缀为 `b2-b4`，移出样本数 192。
- 金额统计口径调整：4 个金额批次产物重算，其余 16 个复用。
- `region` 禁用：只失效 `region` 的 4 个活动产物。
- `device_score` 数值改类别：只失效 `device_score` 的 4 个活动产物。
- 最终自检 6/6 通过，增量快照与全量重建 SHA-256 一致，全部历史告警重放校验和一致。

完整机器可读结果见 `artifacts/demo_report.json` 和 `artifacts/verification.json`。

## 设计取舍

- 缓存条目不做自动淘汰；通过 `max_cache_entries` 明确暴露容量边界。
- 当前实现按配置保守地把检验方式和阈值纳入统计缓存键，因此变更检验配置会同时失效该特征的统计与检验工件。这优先保证键语义完整和可追溯性。
- 默认数据规模较小，便于在本地演示中观察每次重算和逐字节一致性；所有范围由配置决定。
- 浮点逐字节一致性在固定版本、固定平台环境中保证；跨架构运行时应固定 wheel 与数值库版本后重新建立基线。

