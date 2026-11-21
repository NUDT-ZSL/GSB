# wadiag：本地段式日志写入放大诊断与合并裁决

`wadiag` 在本地确定性样本上复现以下完整链路：

1. 校验写入轨迹、存储配置、诊断配置与逻辑时钟步进表。
2. 按内存表容量生成不可变段，原子写入段文件和确定性段清单。
3. 为每段生成按键排序的稀疏索引、自适应布隆摘要和查找代价观测。
4. 计算每层重复率、墓碑占比、压缩比、写入放大贡献与合并增益。
5. 同时运行定点主通路与 Decimal 高精度真值通路，累计误差上界并标记精度敏感裁决。
6. 在单轮合并字节预算和目标层容量约束下选择段，分类记录三种未完成结局。
7. 用 SQLite 事务、版本化层视图指针、pending 操作日志和文件锁支持恢复与多进程并发。

## 环境与安装

目标环境为 Windows + Python 3.11 及以上。Windows PowerShell 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest -q
```

运行时代码只依赖 Python 标准库；`numpy` 和 `pytest` 仅列在开发依赖中。

本次实际验收环境是 WSL/Linux，系统仅有 Python 3.10.12。由于该镜像缺少
`ensurepip`，项目内 `.venv` 先使用 `--without-pip` 创建，再用官方
`get-pip.py` 安装 pip，并在隔离环境中安装 `numpy 2.2.6`、`pytest 9.2.0`：

```bash
python3 -m venv --clear --without-pip .venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
.venv/bin/python /tmp/get-pip.py
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

代码保持 Python 3.10 兼容，但 Windows 验收仍建议使用 3.11 及以上。文件锁实现
在 Linux/WSL 使用 `fcntl.flock`，在 Windows 使用 `msvcrt.locking`。

## 固定样本

- `samples/write_trajectory.json`：36 条确定性写入/删除记录。
- `samples/storage_config.json`：内存表、段大小、层级、预算、压缩比等配置。
- `samples/diagnostic_config.json`：定点标度 6、容差、误差预算、采样比例。
- `samples/clock_steps.json`：逻辑时钟起点、步进和序号覆盖表。

轨迹记录格式：

```json
{"seq": 1, "key": "user:0001", "value_bytes": 80, "op": "put", "clock_ms": 1000}
```

`op` 的闭合枚举为 `put/write/upsert` 和 `delete/del/tombstone`，规范化后只持久化
`put`、`delete`。键必须匹配 `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`。记录序号必须
严格递增且唯一；逻辑时钟覆盖值只替换相同序号的记录时间戳。

## 快速运行

以下命令适用于 Linux/WSL；Windows 将 `.venv/bin/python` 换成
`.venv\Scripts\python`。

```bash
WS=$(mktemp -d)
.venv/bin/python -m wadiag --workspace "$WS" load
.venv/bin/python -m wadiag --workspace "$WS" flush
.venv/bin/python -m wadiag --workspace "$WS" diagnose --query-key user:0007
.venv/bin/python -m wadiag --workspace "$WS" compact
.venv/bin/python -m wadiag --workspace "$WS" status
.venv/bin/python -m wadiag --workspace "$WS" verify
```

`--workspace` 可放在子命令之前或之后。未显式给出的 `load` 样本路径默认指向
`samples/`。所有正常结果和业务错误均输出稳定排序 JSON；错误时退出码为 2，
`verify` 结果为失败时退出码为 1。

## 数据与算法口径

### 内存表与段文件

- 单条记录内存占用为 `UTF-8(key) + value_bytes + 16`，其中 16 字节是固定元数据估算。
- 写入流按该占用累计，达到 `memtable_capacity_bytes` 前切段；随后还受
  `segment_size_limit_bytes` 的硬上限二次约束。单条记录自身超过硬上限时只能独占一段。
- 内存表水位不是简单取最后提交批次，而是取已被已提交段覆盖的轨迹连续前缀末端；
  因此并发先提交高段区间时，水位不会错误跳过未刷盘前缀。
- 段文件名格式为 `S{8位段号}-v{版本}.seg`，位于
  `segments/{run_id}/`；清单位于 `manifests/{run_id}/`。
- 段文件是 UTF-8 确定性字节流：第一行为规范 JSON 头，后续每行是一条规范 JSON
  记录。头中包含 run、段号、版本、层、代数、记录数、块大小和布隆参数。
- 所有写入都使用同目录临时文件、`fsync` 和 `os.replace` 原子重命名。
- 段元数据包含键区间、最新版本、墓碑数、内容摘要、SHA-256、稀疏索引、布隆摘要。
- 同一轨迹在全新工作区重复运行会得到逐段逐字节一致的段文件和清单，已测试。

### 稀疏索引与布隆摘要

- 段记录按键和序号排序后，以 `compression_block_size_bytes` 划分逻辑块。
- 每块保存首键、末键、文件偏移、记录数和最大序号；索引按首键稳定排序。
- 布隆位宽为不小于 64 且按 64 位向上取整的 `key_count * bloom_bits_per_key`。
- 哈希数在配置上限内按 `round(m/n * ln 2)` 自适应；误判率上界用
  `(1 - exp(-k*n/m))^k` 的 Decimal 高精度计算并向上量化到 12 位。
- 精确键查找先按键区间和布隆过滤段，再定位一个索引块。输出包含段号、层号、
  键区间、索引参数、布隆参数、块号和块偏移等观测，重复调用逐位一致。

### 分层写入放大

层内统计只使用已提交段：

- `payload_bytes = UTF-8(key) + value_bytes`
- `materialized_bytes`：所有版本 payload 之和。
- `live_bytes`：每键最高序号且不是墓碑的 payload 之和。
- `reclaimable_bytes = materialized_bytes - live_bytes`
- `duplicate_rate = (occurrences - distinct_keys) / occurrences`
- `tombstone_ratio = tombstone_count / occurrences`
- `wa_contribution = materialized_bytes * compression_ratio / max(1, live_bytes)`
- `merge_gain = reclaimable_bytes * compression_ratio`
- 整库 WA 固定按 L0、L1、... 层序相加。

定点通路在每个比率、压缩比、乘除和最终加法位置按 `ROUND_HALF_UP` 量化到
`fixed_scale`。每个换算节点记录原始值、量化值、标度、舍入方向、舍入余量及层号。
真值通路使用 50 位 Decimal 上下文全量重算，输出保存到 18 位小数。

每层同时给出绝对偏差、相对偏差和保守误差上界。WA 误差界由压缩比量化误差
`materialized/live * comp_error`、最终半单位舍入误差组成；整库再累计层间加法
半单位误差。误差界超过 `error_budget` 时输出 `error_budget_exceeded=true`。

### 精度敏感标记

`diagnose` 比较两类候选：

- 相邻两层的合并增益。
- 每个已触发层中定点排序前两个段的合并增益。

当定点差值或真值差值小于 `float_tolerance`，且至少一个相关层已触发、当前合并
裁决确实依赖该排序时，标记为精度敏感。输出会记录 `within_tolerance`、
`order_changed`、`decision_dependency`、敏感层号和误差来源链。

`compact` 若发现触发的最高收益层候选差距敏感，会记录
`decision_basis=truth_tiebreak_after_sensitive_flag`，并用真值通路作为次序裁决依据。

## 合并与墓碑裁决

当某层段数达到该层触发阈值时，候选段按合并增益降序、段号升序稳定排序。单轮至少
选择足以让该层回落到阈值以下的段数：

```text
desired = len(source_segments) - trigger_threshold + 1
```

目标层容量估算为：

```text
target_threshold * segment_size_limit_bytes
```

每个候选段都会输出 `first_trigger_constraint`，可能值包括层级阈值、单轮字节预算、
目标层容量和选择目标已满足。结局枚举：

- `SELECTED`：在预算与容量内被选中。
- `NOT_SELECTED_WITHIN_BUDGET`：预算足够，但已满足本轮最低选择数。
- `SELECTED_THEN_CANCELLED`：候选曾进入选择过程，但目标层容量约束使其取消。
- `ACTIVE_ABANDONED`：预算不足，主动放弃该候选及后续候选。

墓碑物理清除条件：

```text
output_generation - tombstone.origin_generation >= tombstone_retention_generations
且该键不存在于任何未选中段
```

第二条比“无更低层旧版本”更保守，以避免同层边界或未选中段导致旧版本重新暴露。
只有满足条件时才从合并输出中删除墓碑。

## SQLite、并发与恢复

工作区主要持久化对象：

- `runs`、`records`：轨迹快照和记录。
- `segments`：段元数据、SHA-256、状态与 retired 标记。
- `level_view_versions`：不可变层视图历史。
- `level_view_heads`：每层当前视图版本指针。
- `manifests`：清单 kind、摘要版本、相对路径、段集合和提交状态。
- `pending_ops`、`decisions`：未完成操作与裁决审计。
- `watermarks`：内存表已刷盘序号和逻辑时钟水位。
- `diagnostics`：按层视图摘要保存的稳定诊断快照。

提交协议：

1. 先在 SQLite 事务中登记 pending 操作、段元数据和清单摘要。
2. 再写段文件和清单，全部使用临时文件加重命名。
3. 最后在一个事务中通过 `TEMP TABLE level_stage` 按层号准备完整层视图，为每层
   写入新 `view_version`，并整体替换 `level_view_heads` 指针。

因此读取方只会看到完整已提交的层视图。恢复时只重放状态为 `prepared` 的 pending
操作，按 SQLite 中的持久化载荷补齐缺失文件，再执行同一幂等提交逻辑；只删除
无引用临时文件，不承诺在强杀进程内生成退出报告。

跨进程写操作由 `{workspace}/.wadiag.lock` 文件锁串行化，SQLite 同时使用 WAL、
外键、`BEGIN IMMEDIATE` 和 30 秒 busy timeout。段表唯一键
`(run_id, segment_no, version)` 与层视图主键共同保证重复提交幂等。

### 强制终止测试

测试注入点通过 `WADIAG_CRASH_POINT` 或 `--crash-point` 设置：

- `flush_prepare`：pending 已提交、文件尚未写。
- `flush_files`：段文件与清单已写、层视图尚未提交。
- `compact_prepare`：合并 pending 已提交、输出文件尚未写。
- `compact_files`：合并输出已写、层视图尚未提交。
- `resume_files`：恢复补齐文件后、提交层视图前。

```bash
.venv/bin/python scripts/crash_recovery_test.py
```

该脚本启动真实子进程，在 `flush_files` 注入点调用 `os._exit(137)`，随后执行
`resume` 与 `verify`。

## 子命令

| 命令 | 作用 |
| --- | --- |
| `load` | 校验并载入轨迹、配置与时钟表，非法输入不创建/修改工作区 |
| `flush` | 按内存表规则写段；支持 `--from-segment/--to-segment` 并发区间 |
| `diagnose` | 输出分层 WA、双通路偏差、精度敏感标记、误差链和查找代价 |
| `compact` | 执行一轮预算约束下的合并裁决与提交 |
| `resume` | 补齐并重放 pending 刷盘/合并操作 |
| `status` | 输出层视图版本、水位、pending、清单状态和孤儿段文件 |
| `verify` | 校验段 SHA、确定性重建、层摘要、清单版本和诊断快照 |

主要错误码：

`E_BAD_TRAJECTORY`、`E_BAD_STORAGE`、`E_BAD_LEVELS`、`E_BAD_SCALE`、
`E_BAD_TOLERANCE`、`E_BAD_BUDGET`、`E_BAD_SAMPLE`、`E_BAD_CLOCK`、
`E_BAD_RANGE`、`E_PENDING`、`E_CONFLICT`、`E_CHECKSUM`、`E_STATE`。

## 实际验收结果

本机实际运行结果：

- `.venv/bin/python -m pytest -q`：`28 passed`。
- `scripts/crash_recovery_test.py`：强杀退出码 `137`，恢复前 pending 数 `1`，
  恢复后 pending 数 `0`，`verify_ok=true`。
- 固定样本刷盘：14 个段；清单摘要
  `ad4c03f8b36b000e0b02cfbace14d74f721716f949b7b51458eb573a74a743eb`。
- 初始整库 WA：定点 `3.024348`，真值 `3.024349113043478261`，
  绝对偏差 `0.000001113043478261`，误差界 `0.000004786956521739`。
- 初始诊断已标记精度敏感层 L0；执行六轮合并后进一步标记 L0、L1、L2。
- 六轮合并实际覆盖 `SELECTED`、`NOT_SELECTED_WITHIN_BUDGET`、
  `SELECTED_THEN_CANCELLED`、`ACTIVE_ABANDONED` 四种结局。
- 最终 `verify`：检查 11 个已提交段，`ok=true`，无错误。
- 连续执行两次 `verify` 输出逐字节一致。
- 两个并发进程分别刷写 `1:7` 与 `8:14` 段区间，最终 L0 恰有 14 个唯一段，
  `verify` 通过。
