# SQL 逻辑计划归因与偏差热力实验台

这是一个面向本地 Windows/Python 环境的确定性实验系统。它只用 Python 标准库与本地 SQLite 文件，完成 SELECT 解析、稳定逻辑计划、统计口径快照、逐算子行数估计、真实计数、偏差归因、热力整替、会话快照恢复，以及三类可复现异常注入。

不依赖 Docker、网络、外部数据库服务或第三方 Python 包。

## 快速开始（Windows cmd.exe）

要求 Python 3.11+、`sqlite3`、`tkinter`。GUI 需要 Tk；核心引擎、CLI、测试与实验脚本不需要 Tk。

```bat
python --version
python -c "import sqlite3, tkinter; print(sqlite3.sqlite_version, tkinter.TkVersion)"

python -m unittest discover -s tests -v
python experiment.py --out reports
run_experiment.bat --out reports
```

实验脚本会重建 `reports\experiment.sqlite3`，并写出：

- `reports\experiment_report.json`：完整、排序稳定的机器可读报告。
- `reports\experiment_report.md`：算子归因、口径对比、快照恢复和异常码摘要。
- `reports\anomaly_logs.json`：结构化异常日志。

## Tkinter 界面

```bat
python -m sqlplan_lab --db lab.sqlite3 gui
run_gui.bat
```

界面提供查询编辑、逻辑计划树、逐算子估计/真实行数、偏差热力块、当前数据版本与统计版本、快照列表，以及建样本、生成统计、分析和快照保存/恢复操作。

## 命令行

全局参数 `--db` 放在子命令前：

```bat
python -m sqlplan_lab --db lab.sqlite3 ingest --sql samples\demo.sql
python -m sqlplan_lab --db lab.sqlite3 stats --buckets 8 --sample-rate 1.0
python -m sqlplan_lab --db lab.sqlite3 plan --query-file samples\query.sql
python -m sqlplan_lab --db lab.sqlite3 estimate --query-file samples\query.sql
python -m sqlplan_lab --db lab.sqlite3 analyze --query-file samples\query.sql
```

修改统计口径并比较两个版本：

```bat
python -m sqlplan_lab --db lab.sqlite3 stats --buckets 12 --sample-rate 1.0 --null-override orders.status=0.12 --null-override orders.amount=0.05
python -m sqlplan_lab --db lab.sqlite3 compare --query-file samples\query.sql --old-stats sv_xxx --new-stats sv_yyy
python -m sqlplan_lab --db lab.sqlite3 heatmap
```

快照：

```bat
python -m sqlplan_lab --db lab.sqlite3 snapshot-save baseline
python -m sqlplan_lab --db lab.sqlite3 snapshots
python -m sqlplan_lab --db lab.sqlite3 snapshot-restore baseline
python -m sqlplan_lab --db lab.sqlite3 snapshot-verify
```

异常注入：

```bat
python -m sqlplan_lab --db lab.sqlite3 anomaly skew
python -m sqlplan_lab --db lab.sqlite3 anomaly stats
python -m sqlplan_lab --db lab.sqlite3 anomaly replay --query-file samples\query.sql
python -m sqlplan_lab --db lab.sqlite3 logs
```

`experiment.py --anomaly skew|stats|replay|all` 也可只验证指定异常，默认 `all`。

## 样本与标记

`samples\demo.sql` 包含两张本地表、确定性 INSERT 以及注释形式的元数据标记：

```sql
-- @primary-key customers.id
-- @primary-key orders.id
-- @skew orders.status=paid:0.583333
```

主键标记与 SQLite 实际主键共同记录；倾斜标记用于受控倾斜注入。灌入会先清空当前用户表，再在同一事务中执行样本；任何失败都回滚到灌入前的数据、数据版本和会话状态。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_PARSE` | 不支持的 SELECT、子查询、聚合、函数或非法谓词 |
| `E_DATA` | 样本灌入/真实计数等数据错误；灌入整体回滚 |
| `E_STATS` | 统计版本缺失、损坏、校验失败或统计口径非法 |
| `E_VERSION` | 估计与当前数据版本不一致 |
| `E_SNAPSHOT` | 快照缺失、校验失败、统计版本缺失或无法一致恢复 |
| `E_SKEW` | 受控倾斜放大超过安全阈值 |
| `E_REPLAY` | 计划重放身份或规范化文本不一致 |

## 确定性设计

- SQL 被解析为 AST；大小写、空白、冗余括号以及 AND/OR 结合次序不会改变规范化文本或 `plan_id`。
- 计划节点使用 `op_000` 起的拓扑序 ID；树与节点数组都按稳定顺序序列化。
- 数据版本 `dv_*` 由表结构、类型标记后的行内容和标记的 SHA-256 内容摘要生成，不依赖插入物理顺序。
- 统计版本 `sv_*` 由数据版本、口径参数和统计内容摘要生成；同一输入重复生成相同版本。
- 估计使用整数半入舍入；偏差分数是 0..10000 的定点值，真实行数为 0 时比值使用 `ZERO_ACTUAL` 哨兵。
- 热力刷新先删除全部旧行，再写入当前计划全部算子，不做增量合并。
- 快照保存完整会话与表结构元数据；恢复先校验统计/数据/计划，再整体替换会话和热力表。
- 异常日志不含随机数或时间戳，事件 ID 由结构化载荷摘要生成，重复注入得到相同 ID。

更多实现细节见 `TECHNICAL.md`，最近一次实际验证结果见 `VALIDATION.md` 与 `reports\experiment_report.md`。

## 目录

```text
sqlplan_lab\       解析器、计划器、统计、引擎、异常、CLI、GUI
samples\           本地 SQL 样本与查询
tests\             unittest 自动测试
experiment.py      可重复端到端实验
run_experiment.bat Windows cmd 启动脚本
run_gui.bat        Windows GUI 启动脚本
reports\           实际验证生成的压力/一致性报告
```
