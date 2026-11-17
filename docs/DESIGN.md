# 聚合与窗口函数流式管线 — 技术说明

仅依赖 Python 3.11+ 标准库（`json`、`sqlite3`、`argparse`、`threading`、`logging`、
`unittest`、`hashlib`、`dataclasses`）。目标运行环境为 Windows 本地，所有路径使用
`os.path` 拼接，不依赖容器、网络或第三方包。

## 1. 目录结构

```
aggwin/
  __init__.py     状态格式版本、定点口径版本常量
  errors.py       业务码 E_SCHEMA/E_EXPR/E_SNAPSHOT/E_BATCH/E_ORDER/E_DIVERGE 与退出码
  fixedpoint.py   定点整数均值（缩放因子、向零截断、量化误差上界）
  schema.py       输入表 JSON 强校验（表/行/字段定位）
  queries.py      结构化查询 + 受限 SQL 方言解析、规范化标识、计划编译
  ring.py         定长环形缓冲（进入/滑出/乱序/局部重算）
  state.py        逐分组聚合状态、逐分区窗口状态
  snapshot.py     SQLite 快照、版本比对、整体替换、损坏检测
  pipeline.py     流式引擎：分批推进、检查点、恢复、报告
  verifier.py     独立参考重算 + 快照交叉校验 + 首差异定位
  gui.py          Tkinter 界面（延迟导入，无显示环境降级）
  cli.py          命令行、结构化 JSON 日志、故障注入开关
  demo.py         子进程验收脚本（12 个场景）
sample/           确定性样本表与查询（含乱序、单调两张表）
tests/            unittest 测试套件（15 个用例）
docs/             技术说明
```

## 2. 数据与查询模型

### 2.1 输入表

顶层 `tables` 列表，表名必须唯一。列类型仅支持 `timestamp / int / long / double /
string`；标识符须匹配 `^[A-Za-z_][A-Za-z0-9_]*$` 且非 Python 关键字。每张表必须
声明一个 `timestamp` 类型的 `timestamp_column`。`ordering` 显式声明：

- `monotonic`：加载期发现时间戳回退立即 `E_SCHEMA`，定位到表、行号、字段；
- `out_of_order`：允许乱序，`allowed_lag` 为窗口允许的迟到时间戳宽度，
  `ooo_ratio_limit` 为运行期乱序比例上限，超限触发 `E_ORDER`。

任一字段非法（含 null、类型不符、列数不符、重复表名/列名）整体拒绝加载，错误
上下文带 `table`、`row`、`field` 定位。

### 2.2 查询与规范化标识

聚合：`COUNT / SUM / MIN / MAX / MEAN`，按 `group_by` 分组；窗口：按
`partition_by` 分区、按时间戳 `order_by` 排序的 `SUM` 与 `ROW_NUMBER`，帧由
`lower/upper` 整数偏移与 `max_rows` 双约束定义，区间为左右闭区间
（`inclusive_both_bounds`，写入状态与轨迹）。

规范化规则：标识符统一小写折叠，分组/分区键排序，输出固定模板的 canonical 文本，
再对文本取 SHA-256 作为结构摘要。空白差异、键顺序差异、大小写差异对同一语义查询
产生完全相同的 canonical 与 digest。canonical/digest 写入执行轨迹 `trace` 与快照
`trace` 表。

受限 SQL 方言（单条 SELECT）被编译为同一内部表示；括号内再次出现 SELECT 判定为
子查询，`WHERE/JOIN/HAVING/UNION/WITH/ROWS` 等不支持构造一律 `E_EXPR`。

## 3. 定点均值口径

`mean_scaled = trunc_toward_zero(sum / count * SCALE)`，默认
`SCALE = 10000`，口径版本 `FP-V1`，截断方向 `TO_ZERO`，全部为整数运算，无浮点
参与状态。量化误差严格满足 `|err| < 1/SCALE = 1e-4`。状态摘要携带
`fixed_point_version` 与 `fixed_point_scale`。

各算子只维护其语义所需字段（COUNT 仅计数；SUM 维护 count/sum；MIN/MAX 维护
count/极值；MEAN 维护 count/sum/定点均值），同一行以 `seen_row_ids` 去重，重复
推送不改变任何状态，因此批次边界不影响最终结果。

## 4. 环形缓冲与乱序规则

`RingBuffer` 使用定长数组 + head/size 实现真环形结构：

- 进入：新行入环，若达到 `max_rows` 先淘汰最旧行；
- 滑出：每到达一行先按 `current_ts + lower` 淘汰严格早于下界的行，滑出只从总和中
  减去被淘汰行的贡献，不影响仍在帧内的行；
- 乱序：迟到行满足 `ts >= watermark - allowed_lag` 才准入，准入后触发
  `recompute_sum()` 局部重算并在发射记录标记 `late=true`；超过滞后宽度的行被拒绝，
  发射 `admitted=false` 且原因 `beyond_allowed_lag`；
- 序号：帧内行按 `(ts, row_id)` 稳定排序后给出 1 基 `ROW_NUMBER`。

序列化时 head 归一化为 0、按逻辑到达顺序输出 entries，因此摘要与快照不依赖内部
旋转位置。

## 5. 快照、恢复与整体替换

每个检查点写入一个自包含 SQLite 文件（`meta/cursors/agg_states/win_states/
emissions/trace` 六张表），并通过 `latest.snapshot` 指针文件指向最新快照。恢复时
比对三项版本：

1. `state_format_version`；
2. `fixed_point_version`（含 scale）；
3. 查询参数 `params_digest`（全部 canonical 排序拼接后的 SHA-256）。

任一不一致即**整体丢弃**内存中的分组与窗口状态、游标归零重放，绝不做增量合并，
报告标记 `replaced_state=true`。一致则从每表游标继续推进。损坏检测覆盖文件缺失/
截断、`PRAGMA integrity_check` 失败、必需表缺失、JSON 反序列化失败，统一
`E_SNAPSHOT`。损坏只作用于被截断的最新文件，历史快照仍可被验证工具读取。

## 6. 逐位一致性与首差异定位

`verifier` 不依赖引擎状态类，独立从原始表/查询重算聚合与窗口参考结果，再与结果
文件和快照交叉核对四项：聚合一致、窗口一致、快照版本与游标、状态摘要与输出对应。
比较器深度递归，返回首个不一致的定位路径、期望值与实际值，业务码 `E_DIVERGE`，
退出码 7。恢复运行与不中断运行的 `aggregates.json/windows.json/trace.json` 逐字节
相同；`report.json` 仅排除与分批调度内在相关的 `batch_events/checkpoints` 后做
规范化逐字节比较，业务状态（含归一化后的环形缓冲）完全一致。

## 7. 故障注入（只改文件/参数，不改源码）

| 开关 | 业务码 | 退出码 | 机制 |
| --- | --- | --- | --- |
| `--inject-mid-window [--strict-batch]` | E_BATCH | 5（strict 时） | 检测批次边界落在未闭合窗口帧内，写结构化日志 |
| `--ooo-ratio-limit 0` | E_ORDER | 6 | 覆盖表级乱序比例阈值，首个乱序行即超限 |
| `--inject-corrupt-snapshot` | E_SNAPSHOT | 4 | 检查点落盘后截断最新快照，恢复时拒绝 |

结构化日志为单行 JSON（`level/event/code/context`），同时输出 stderr 与
`--log-file`。错误发生时向结果目录写 `error.json`，已持久化快照保持可读。

## 8. Tkinter 界面

`gui` 子命令在后台线程加载文件，避免阻塞 UI；主区按稳定顺序展示查询 canonical/
digest、游标、逐分组聚合状态、逐分区窗口内容、快照文件名列表。Tkinter 采用函数内
延迟导入，缺少显示或缺少 Tkinter（如本 Linux 沙箱）时抛 `HeadlessUnavailable`，
引擎、CLI、验证器与测试不受影响；Windows 官方 Python 安装包默认包含 Tkinter。

## 9. 业务码与退出码

| 业务码 | 含义 | 退出码 |
| --- | --- | --- |
| E_SCHEMA | 表结构/数据强校验失败 | 2 |
| E_EXPR | 查询语法/算子/子查询不支持 | 3 |
| E_SNAPSHOT | 快照损坏或不可读 | 4 |
| E_BATCH | 批次边界落在开窗中途 | 5 |
| E_ORDER | 乱序比例超限 | 6 |
| E_DIVERGE | 逐位一致性校验出现差异 | 7 |
