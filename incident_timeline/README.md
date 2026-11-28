# 故障事件时间线汇总组件（incident_timeline）

把告警、发布、探测、值班备注、用户投诉五类多源事件，按逻辑时刻与服务标识归一，
压缩为按故障聚合的时间线，并产出可交接的摘要、合并留痕、去重剔除依据与数量账目，
全部结果持久化到 SQLite，支持追加批次后的增量重算。

- 仅使用 Python 3 标准库（`sqlite3`、`argparse`、`json`、`dataclasses`、`unittest` 等）。
- 无需第三方包、无需外部数据库服务。
- 固定随机种子样本，结果可重复；增量结果与全量重算逐字段比对。

## 目录结构

```
incident_timeline/
├── config/dependency_registry.json      # 四类服务共用的依赖登记（边 A->B 表示 B 是 A 的下游）
├── samples/                             # 固定种子样本与人工标注真值（运行生成脚本后出现）
├── scripts/
│   ├── generate_samples.py              # 固定种子事件样本 + 真值生成
│   ├── compare_runs.py                  # 全量 vs 增量逐字段比对
│   └── run_all.sh                       # 一键复现完整流程
├── src/incident_timeline/
│   ├── normalize.py     # 要求1：读取/校验/归一化，未登记服务给出行号级错误
│   ├── dedup.py         # 要求5：告警风暴去重，保留首次、记录剔除依据与原始编号
│   ├── clustering.py    # 要求2：同服务候选簇 + 依赖关联并查集合并，输出每次合并依据
│   ├── faults.py        # 要求3/4：起止边界、未闭环判定、根因排序、传递闭包影响面、佐证标注
│   ├── accounting.py    # 要求6：按来源/按服务的数量账目与对账
│   ├── timeline.py      # 要求7：时间线、关键事件选择依据、倒序/重叠矛盾检查
│   ├── storage.py       # 要求8：SQLite 建表与版本化持久化
│   ├── pipeline.py      # 全量/增量共用的确定性计算链路与边界变化识别
│   ├── export.py        # 导出 JSON 与可读文本报告
│   └── cli.py           # argparse 入口（run / export）
└── tests/test_pipeline.py               # 固定种子真值验收
```

## 输入数据格式

事件文件为 JSON Lines，每行一条事件，至少包含：

| 字段 | 含义 |
| --- | --- |
| `event_id` | 全局唯一原始事件编号（去重剔除也保留该编号） |
| `source` | `alert` / `release` / `probe` / `note` / `complaint` |
| `service` | 服务标识，必须出现在依赖登记中 |
| `code` | 事件码 |
| `ts_ms` | 毫秒级逻辑时刻（非负整数） |
| `batch` | 批次标识（可被命令行默认批次覆盖） |
| `status` | `trigger` / `recovery` / `info`（发布回滚用 `rollback`） |

依赖登记 `dependency_registry.json` 列出全部服务与有向依赖边。
边 `A -> B` 表示 A 异常会沿依赖链传导到 B，即 B 是 A 的下游。

## 窗口参数（可在命令行覆盖）

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--storm-window-ms` | 60000 | 同服务同事件码重复触发的告警风暴去重窗口 |
| `--proximity-window-ms` | 120000 | 同服务事件归入同一候选簇的邻近窗口 |
| `--merge-window-ms` | 120000 | 跨服务候选簇按依赖关联合并的时间窗 |

## 本地运行步骤

### 方式一：一键复现

```bash
cd incident_timeline
bash scripts/run_all.sh
```

该脚本依次执行：生成固定种子样本 → batch1 全量汇总写 SQLite →
追加 batch2 增量重算 → 对追加后文件做独立全量重算作为对照 →
逐字段比对增量与对照全量 → 运行真值验收测试。

### 方式二：分步执行

```bash
cd incident_timeline
export PYTHONPATH=src

# 1. 生成固定种子事件样本与人工标注真值
python3 scripts/generate_samples.py

# 2. 全量汇总并写入 SQLite，同时导出时间线与账目
python3 -m incident_timeline.cli run \
  --events samples/events_batch1.jsonl \
  --registry config/dependency_registry.json \
  --db data/incident.db \
  --export-dir data/exports

# 3. 追加新批次后增量重算
python3 -m incident_timeline.cli run \
  --events samples/events_batch1_plus_batch2.jsonl \
  --registry config/dependency_registry.json \
  --db data/incident.db \
  --incremental-batch batch2 \
  --export-dir data/exports

# 4. 单独导出某次运行（默认最新一次）
python3 -m incident_timeline.cli export \
  --registry config/dependency_registry.json \
  --db data/incident.db \
  --out-dir data/exports --run-id 2

# 5. 运行验收测试
python3 -m unittest tests.test_pipeline -v
```

## 八项业务要求的实现落点

1. **归一化与登记校验**：`normalize.py` 统一来源、服务、规范化毫秒时刻；
   未登记服务抛出带 `文件:行号(event_id=...)` 的 `EventValidationError`，不静默丢弃。
2. **聚类与可复核合并**：`clustering.py` 先按服务与邻近窗口构建候选簇
   （恢复事件优先回挂同事件码未闭环候选，恢复后强制收尾开新簇），
   再用并查集对「跨簇相邻事件时间差与范围间隔都不超过窗口、且存在跨服务依赖链」
   的候选簇合并；`merge_records` 记录每一步两个簇编号、依赖路径与时间差。
3. **边界与未闭环**：`faults.py` 以簇内最早/最晚事件为初值，结合告警/探测恢复修正结束时刻；
   缺少同服务同事件码恢复的触发进入 `open_triggers`，簇标记 `is_closed=false`，
   结束时刻只取簇内观测最晚事件，绝不伪造成观测窗口末端。
4. **根因与影响面**：按依赖登记传递闭包计算下游；根因候选以最早异常时刻为主、
   异常数量与窗口内发布相关性为辅排序；受影响下游只保留位于根因依赖链上的簇内服务，
   无依赖关系的服务进入 `not_on_dependency_chain`，不计入影响面。
5. **去重与佐证**：`dedup.py` 对同服务同事件码风暴窗口内的重复触发首次保留、后续计入
   `dedup_records`（含被剔除与保留的原始事件编号、时间差、依据）；
   值班备注与用户投诉不去重，标注为佐证事件挂载到同服务簇。
6. **数量账目**：`accounting.py` 按来源与按服务给出原始、去重后、重复剔除、入故障簇、
   上下文簇、佐证、未挂载佐证各计数；`reconciliation` 强制合计等于输入总条数，
   任一类差额必须由剔除记录解释，否则 `all_ok=false`。
7. **时间线**：`timeline.py` 按开始时刻排序输出每簇起止、涉及服务、影响面摘要与关键事件，
   每条关键事件都带选择依据；同时检查时间倒序与簇范围重叠，矛盾写入 `timeline_conflicts`。
8. **SQLite 与增量**：`storage.py` 版本化保存每次运行；`pipeline.run_incremental`
   与全量共用同一确定性计算链路，按事件归属匹配新旧故障，标记
   `new` / `boundary_changed` / `verified_unchanged` / `disappeared`，
   未受影响簇的时间线与账目逐字段比对，`compare_runs.py` 可独立复核。

## 验收测试

`tests/test_pipeline.py` 基于 `samples/ground_truth.json` 人工标注真值，覆盖：

- 聚类边界、根因排序、影响面、未闭环标识；
- 去重剔除编号集合、佐证事件归属、上下文簇识别；
- 账目合计与差额可解释性；
- 时间线无倒序/重叠、关键事件选择依据完整；
- 合并留痕可追溯；
- 未登记服务的可定位报错；
- 增量与全量在故障簇、时间线、账目三个层面的逐字段一致性，以及边界变化识别。
