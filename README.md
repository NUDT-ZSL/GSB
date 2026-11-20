# colagg：本地列式聚合与可恢复实验引擎

`colagg` 是一个只依赖 Python 标准库、NumPy 和本地文件系统的确定性列式聚合引擎。它流式读取列式 JSON 批次，在固定算子状态预算下执行分组聚合和跨批次滑动窗口，必要时把分组状态真实写入本地 spill 文件，并通过 SQLite 事务保存可恢复的提交水位。

本项目的“内存预算”指引擎显式记账的算子常驻状态，包括分组键、统计量、窗口缓冲和固定开销；Python 解释器、NumPy 与 SQLite 自身的运行时开销不在该记账范围内。每次状态增长后都会执行预算检查，`status` 中的 `peak_resident_bytes` 始终不超过 `memory_budget_bytes`。单分组自身超过预算时直接返回 `E_STATE_TOO_LARGE`，不会用延迟或超时伪装隔离。

## 环境

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m pytest -q
```

固定版本：

- Python 3.11.15
- NumPy 1.26.4
- pytest 8.3.3

Windows 下使用 `.venv\Scripts\python.exe`，并设置 `set PYTHONPATH=.`。文件锁在 POSIX 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`；落盘使用同目录临时文件、`fsync` 和 `os.replace` 原子重命名。

## 工作区

```text
colagg/                  引擎、CLI、校验、持久化与验证
samples/                 固定随机种子样本（字节级固定）
scripts/generate_samples.py
workspace/               SQLite 工作区（运行时生成 colagg.sqlite3）
spill/                   原子 spill 文件（运行时生成）
tests/                   端到端、异常、恢复和并发测试
```

样本由种子 `20261121` 生成：

```bash
PYTHONPATH=. .venv/bin/python scripts/generate_samples.py --out samples
```

样本包含 12 个批次、每批 400 行、200 个 `(site, tier)` 分组、8 行窗口、4 行步长，以及 4 个 NaN 和 5 个无穷值。`samples/injections.json` 是空注入清单；另有异常和崩溃清单。

提交内 17 个样本 JSON 的相对路径与字节内容合并摘要为 `c1ae31113dae82350f72cb8698fc1992dca1b0bb8212f37cd1de4a50436e6b18`；测试会重新生成并逐文件比较。

## CLI

```bash
# 校验并加载计划。任何非法输入在打开 SQLite 前失败，不留下持久化状态。
PYTHONPATH=. .venv/bin/python -m colagg.cli load \
  --query samples/query.json \
  --config samples/config.json \
  --manifest samples/injections.json \
  --batches-dir samples/batches

# 处理所有待处理批次；--once 只处理一个，--batch N 会先补齐其缺失前驱。
PYTHONPATH=. .venv/bin/python -m colagg.cli execute

# 清理强制终止留下的半写/无引用 spill 文件，再继续处理待处理批次。
PYTHONPATH=. .venv/bin/python -m colagg.cli resume

# 稳定排序 JSON：分组版本、失败态、spill/readback 统计、逻辑时钟水位。
PYTHONPATH=. .venv/bin/python -m colagg.cli status

# 校验摘要、spill 文件、状态行，并对提交批次做聚合与窗口全量重算。
PYTHONPATH=. .venv/bin/python -m colagg.cli verify
```

用 `--mode float` 在 `load` 时覆盖配置的数值通路。不同数值通路应使用不同工作区，避免混用状态：

```bash
python -m colagg.cli load ... --mode float --workspace workspace/float.sqlite3
```

退出码：

- `0`：命令成功且无失败态；`verify` 通过
- `2`：业务错误，输出 `{"ok":false,"error":{...}}`
- `3`：执行完成但存在批次或分组失败态
- `4`：`verify` 发现摘要、状态或全量重算分歧
- `86`：注入的 `crash_after_spill` / `crash_before_commit` 强制终止

## 输入格式

### 批次

```json
{
  "batch_seq": 1,
  "row_count": 3,
  "columns": [
    {"name": "site", "type": "string", "values": ["A", "A", "B"]},
    {"name": "tier", "type": "int", "values": [0, 0, 1]},
    {"name": "value", "type": "float", "values": [1.25, "NaN", "Infinity"]},
    {"name": "ok", "type": "bool", "values": [true, false, true]}
  ],
  "batch_digest": "..."
}
```

允许的列类型为 `int`、`float`、`bool`、`string`。JSON 原生不能表示非有限浮点，因此浮点列使用字符串 `"NaN"`、`"Infinity"`、`"-Infinity"`。`batch_digest` 是以下对象的规范化 JSON（键排序、无多余空白、UTF-8）的 SHA-256：

```json
{"batch_seq": 1, "row_count": 3, "columns": [...]}
```

校验覆盖列名唯一、数组长度等于行数、批次序号在输入顺序中严格递增、列类型跨批次一致、摘要匹配、引用列存在、聚合列类型可比、窗口参数和预算合法。

### 查询

```json
{
  "query_id": "deterministic-group-window-v1",
  "group_by": ["site", "tier"],
  "aggregates": [
    {"name": "value_sum", "op": "sum", "column": "value"},
    {"name": "value_mean", "op": "mean", "column": "value"},
    {"name": "value_variance", "op": "variance", "column": "value"},
    {"name": "row_count", "op": "count", "column": "value"}
  ],
  "window": {
    "length": 8,
    "stride": 4,
    "column": "value",
    "outputs": ["count", "sum", "mean", "variance"]
  }
}
```

窗口按每个分组内的到达行序编号：起点为 `0, stride, 2*stride, ...`，只有完整窗口才产出。跨批次未满窗口保留在分组状态中，后续批次补齐后再提交。`count` 表示观测行数；NaN 和无穷分别计数，但不参与求和/均值/方差。

### 配置

```json
{
  "memory_budget_mib": 1,
  "max_batch_rows": 400,
  "spill_dir": "spill",
  "fixed_scale": 1000,
  "fixed_int_bits": 64,
  "float_tolerance": 1e-12,
  "logical_clock_step": 1,
  "numeric_mode": "fixed"
}
```

`memory_budget_mib` 是正整数 MiB。`spill_dir` 必须是工作区根内的相对路径。`logical_clock_step` 是所有计时和窗口水位使用的逻辑时钟步进。

### 注入清单

```json
{
  "injections": [
    {"id": "read-full-2", "phase": "read", "batch_seq": 2,
     "kind": "malformed_column", "column": "value"},
    {"id": "digest-4", "phase": "read", "batch_seq": 4,
     "kind": "digest_mismatch"},
    {"id": "fixed-6", "phase": "aggregate", "batch_seq": 6,
     "kind": "fixed_overflow", "group": ["S00", 0]},
    {"id": "window-8", "phase": "window", "batch_seq": 8,
     "kind": "window_corrupt", "group": ["S01", 1]},
    {"id": "spill-10", "phase": "write", "batch_seq": 10,
     "kind": "spill_tamper", "group": ["S02", 2]},
    {"id": "readback-12", "phase": "read", "batch_seq": 12,
     "kind": "spill_readback_corrupt", "group": ["S03", 3]}
  ]
}
```

还支持 `crash_after_spill` 和 `crash_before_commit`。崩溃注入在退出前写入一次性标记，避免 `resume` 在同一批次无限重复崩溃。

## 数值通路

### float64

按到达顺序直接累加 `float`，结果同时保存十进制数值和 `float.hex()`，用于验证逐位一致。NaN 和无穷被计数并从有限统计中排除。

### 定点

每个值用 `Decimal.from_float` 乘以 `fixed_scale`，以 `ROUND_HALF_EVEN` 量化到整数，并检查配置的 `fixed_int_bits` 有符号边界。`conversion_audit` 保存：

- 批次、行号、列名
- 换算前原始浮点的十六进制表示
- 标度
- 量化后的整数
- 舍入方向 `up` / `down` / `none` / `nonfinite`
- NaN、正无穷、负无穷分类

分组状态保存整数和与平方和；输出中同时提供精确有理数 `exact` 和 `float64` 近似值。两条通路都不静默丢弃非有限值。

## 状态、spill 与恢复

每个批次的执行过程是：

1. `BEGIN IMMEDIATE` 获取 SQLite 写事务。
2. 读取并校验批次，应用读取阶段注入。
3. 按行加载分组状态；若状态在 SQLite 或 spill 中则按需回读。
4. 更新计数、和、平方和、均值可用状态、窗口缓冲和已完成窗口。
5. 状态记账超过预算时，选最久未更新的分组真实落盘，之后需要时再回读。
6. 在单个事务中写入分组状态、分组提交版本、结果、失败记录、spill 清单和逻辑时钟。
7. `COMMIT` 后读取方才能看到该批次和所有分组更新。

spill 文件使用确定性内容摘要命名，在文件锁内只物化一次；写入使用同目录临时文件、`fsync`、`os.replace` 和目录 `fsync`。回读校验 SHA-256。强制终止时 SQLite 自动回滚未提交事务，已落盘但未引用的文件成为孤儿；`resume` 只清理这些无引用文件和无引用清单行，不重新累加已提交分组。

`group_commits(mode, batch_seq, group_hash)` 的主键和 SQLite 事务共同保证同一批次同一分组版本只提交一次。重复提交已提交批次直接作为幂等跳过。

## 验证与全量真值

`verify` 执行两类检查：

1. 持久化一致性：批次摘要、分组状态摘要、分组版本、spill 文件 SHA-256、全局 `state_digest`、重复分组提交。
2. 全量重算：从所有已提交批次重建每分组序列，重新计算聚合和完整窗口，与结果表逐字段比较。窗口不一致时输出稳定排序后的首个分歧，包括分组、窗口起点、期望值、实际值和中间求和值。

失败隔离：

- 读取阶段的畸形列或摘要错误使该批次失败，不提交该批次任何分组状态。
- 聚合、窗口或 spill 阶段的单分组异常只把目标分组置为失败态，其他分组在同一事务中正常提交。
- 已提交的历史结果不会因后续批次失败而回滚。

## 实际验证结果

环境：Python 3.11.15、NumPy 1.26.4、pytest 8.3.3。

固定样本正常执行（`fixed` 通路）：

- 12/12 批次提交，4,800 行，200 个分组
- 峰值记账内存 1,048,576 字节，未超过 1 MiB
- 602 次真实 spill、577 次真实回读；逻辑时钟 2,370
- 1,000 个分组聚合结果、927 个窗口结果
- 9,600 条换算审计，其中向下舍入 2,397 条、向上舍入 2,352 条
- `verify.ok=true`，`state_digest=2708c0bc0af55f71b2bbe6f26083197a120427df306b4e11ee7ca27b5f253db5`

同一固定样本 `float` 通路：

- 同样提交 12/12 批次，927 个窗口，`verify.ok=true`
- NaN 4 个、无穷 5 个均记录；转换审计标度为 0、方向为 `float64`

两次独立干净运行的 `status` 与 `verify` stdout 字节完全一致；`fixed` 和 `float` 各自重复运行均满足该条件。

异常清单结果：

- 10 个批次提交，2 个读取批次失败
- 观察到不同业务码：`E_MALFORMED_COLUMN`、`E_DIGEST_MISMATCH`、`E_FIXED_OVERFLOW`、`E_WINDOW_MISMATCH`、`E_SPILL_TAMPERED`、`E_SPILL_READBACK_FAILED`
- 只有 4 个目标分组进入失败态，其余 196 个分组继续提交
- `verify` 报出窗口 `["S01", 1]`、起点 4 的首个分歧，期望 sum 为 `4032.788`、实际 sum 为 `4033.788`

强制终止结果：

- 在第 5 批次 spill 落盘后强制退出，退出码 86；当时已提交 4 批、待处理 8 批
- `resume` 清理 220 个孤儿 spill 文件和 161 条无引用清单行
- 恢复后 12/12 批次提交，`state_digest` 和逻辑时钟与未终止的干净运行完全一致，`verify.ok=true`

并发结果：两个进程同时对同一计划执行 `--once`，分别提交不同批次；`group_commits` 无重复，读取方只看到提交后的完整状态，`verify.ok=true`。

运行完整测试：

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q
```

当前测试集共 29 项，全部通过。
