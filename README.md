# 审计事件关联与取证工具

一个完全离线的确定性审计事件关联工具。它从本地 JSONL 流读取事件，处理乱序、重复与丢失，重建 trace 内因果链，按可配置规则跨 trace 聚合取证案件，并通过带校验和的检查点与独立案件快照支持崩溃后恢复。

实现只使用 Python 标准库。运行时不需要网络、账号、外部服务或 Docker。

## 实现范围

- JSONL 逐行规范化，字段类型、时间戳和序列号范围校验。
- 畸形行按行号与业务错误码拒绝，后续行继续处理。
- `event_id` 全局去重，首次出现为真值；完全重复与内容冲突分别报告。
- trace 内父子关系、孤儿父事件、自环、长环、跨 trace 父引用检测。
- seq 到达顺序回退告警、重复 seq 与 seq 空洞检测。
- 基于规则连通分量的跨 trace 案件聚合，结果与到达顺序无关。
- 检查点代次、结构版本、规则版本/指纹和 SHA-256 校验。
- 检查点与案件快照分离，使用临时文件、`fsync`、`os.replace` 原子替换。
- POSIX `flock` 与 Windows `msvcrt.locking` 非阻塞文件锁。
- `import`、`query-case`、`export-report`、`check-checkpoint` 四个 CLI 子命令。
- 确定性样本生成器、一键演示脚本和 pytest 测试集。

## Windows 环境准备

目标环境为 Windows 工作站与 Python 3.11 或以上版本。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

在离线机器上从本地 wheel 目录安装 pytest，不使用网络：

```powershell
python -m pip install --no-index --find-links .\wheelhouse pytest
```

如果 pytest 已位于本地 pip 缓存，也可直接使用组织内约定的离线安装方式。工具本身不需要安装第三方包，在项目根目录直接运行：

```powershell
python -m audit_forge --version
```

## 快速开始

生成确定性本地样本：

```powershell
python scripts\generate_samples.py --output-dir samples
```

导入顺序事件：

```powershell
python -m audit_forge import `
  --workspace .\work\ws-ordered `
  --input .\samples\events_ordered.jsonl `
  --checkpoint-every 1
```

导入乱序或含重复投递的事件：

```powershell
python -m audit_forge import `
  --workspace .\work\ws-shuffled `
  --input .\samples\events_shuffled.jsonl `
  --checkpoint-every 1

python -m audit_forge import `
  --workspace .\work\ws-duplicates `
  --input .\samples\events_duplicates.jsonl `
  --checkpoint-every 1
```

查询案件：

```powershell
python -m audit_forge query-case --workspace .\work\ws-ordered
python -m audit_forge query-case --workspace .\work\ws-ordered --case-id case-...
```

导出完整取证报告：

```powershell
python -m audit_forge export-report `
  --workspace .\work\ws-ordered `
  --output .\work\forensics-report.json
```

校验检查点、修复过期派生快照：

```powershell
python -m audit_forge check-checkpoint --workspace .\work\ws-ordered
python -m audit_forge check-checkpoint --workspace .\work\ws-ordered --repair
```

所有命令默认把 JSON 报告写到 stdout；还可加 `--report <path>` 原子写出命令报告。

## 命令行与退出码

| 子命令 | 关键参数 | 作用 |
| --- | --- | --- |
| `import` | `--workspace`, `--input`, `--rules`, `--checkpoint-every`, `--report` | 逐行导入并在指定事件间隔及结束时保存检查点 |
| `query-case` | `--workspace`, `--case-id`, `--rules`, `--report` | 列案件或查询单个案件的证据与因果路径 |
| `export-report` | `--workspace`, `--output`, `--rules`, `--report` | 导出事件、异常、trace 因果链和完整案件 |
| `check-checkpoint` | `--workspace`, `--rules`, `--repair`, `--report` | 校验检查点、快照、版本和临时文件 |

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功。可能出现重复投递、乱序到达等 warning，但均在报告中列出 |
| `1` | 部分失败或业务错误。存在拒绝行、error 级异常、锁冲突、损坏状态、缺少参数目标等 |
| `2` | CLI 参数错误，JSON 报告中的错误码为 `E_ARGS` |

强制终止或 `os._exit` 不保证产生退出报告。这是有意设计：恢复依据只能是最后一个已完整原子提交的检查点。

## 规范化与异常语义

### 必填字段

`event_id`、`trace_id`、`actor`、`action`、`object` 必须为非空字符串；`ts`、`seq` 必须为整数且不得为布尔值；`parent_event_id` 可省略或为 `null`。

- `0 <= ts <= 253402300799999`，即以毫秒表示的 9999-12-31 结束边界。
- `1 <= seq <= 2^63 - 1`。

畸形行不会中断导入。拒绝记录包含 `source`、`line` 和业务错误码。

### 重复与冲突

- 完全相同的重复投递：`E_DUPLICATE_EVENT`，warning，保留首次事件。
- 同 `event_id` 内容不同：`E_EVENT_CONFLICT`，error，保留首次事件并列出冲突字段。
- 重复项仍进入异常报告，不静默丢弃。

### seq

- 到达时低于该 trace 现有最大 seq：`E_SEQ_REGRESSION` / `arrival_regression`，warning；事件仍被接受。
- 去重后同 trace 多个 event 使用同一 seq：`E_SEQ_REGRESSION` / `duplicate_seq`，error。
- seq 从 1 开始存在缺失：`E_SEQ_GAP`，error，报告缺失区间。

`max_seq` 和 `contiguous_seq` 水位都会写入检查点。水位只用于恢复进度与诊断，不作为乱序事件的丢弃条件。

### 因果链

- 父事件不存在：`E_ORPHAN`。
- 父事件是自身或形成长环：`E_CYCLE`。
- 父事件存在于其他 trace：`E_CROSS_TRACE`，同时给出两个 trace。

报告中的每个 trace 包含 `roots`、`edges`、`chains`、逐事件 `ancestry`/`depth` 和该 trace 的因果异常。环中节点的 `ancestry` 为 `null`，并保留完整异常记录。

## 确定性案件聚合

规则文件示例：

```json
{
  "version": "default-v1",
  "rules": [
    {
      "id": "same-actor-object",
      "match_fields": ["actor", "object"],
      "window_ms": 5000
    }
  ]
}
```

`match_fields` 支持 `actor`、`action`、`object`、`trace_id`。同一规则下，匹配字段相同且时间差不超过 `window_ms` 的事件形成连通分量；不同规则产生的连通分量取并集。至少两个事件的连通分量成为一个案件。

案件计算不依赖到达顺序：

- 事件先按确定性规范键 `(ts, trace_id, seq, event_id)` 排序。
- 规则按时间排序后构建连通分量。
- 案件 ID 是规则版本、规则内容指纹、规则 ID 与排序后成员 `event_id` 的 SHA-256 派生值。
- 因此同一去重事件集合在任意合法到达顺序下具有相同的案件键和成员。

规则 `version` 或规则内容指纹变化时，旧案件快照不会被复用，而是从检查点中的事件重新构建。即使配置版本号没改但内容变化，指纹也会触发重建。

## 检查点、快照与恢复

工作目录包含：

```text
checkpoint.json      # 主状态与 cases_ref
cases.json           # 独立案件快照
.audit-forge.lock    # 进程锁文件
```

`checkpoint.json` 保存：

- 结构版本 `schema_version` 与代次 `generation`。
- 完整规范化事件集合、首次出现来源、拒绝记录、导入异常。
- `max_seq` / `contiguous_seq` 水位与统计。
- 当前规则文档、规则版本与内容指纹。
- 案件快照代次、快照校验和与案件内容校验和。
- 对检查点主体计算的 SHA-256 校验和。

`cases.json` 自己也有格式、结构版本、代次、规则版本/指纹和 SHA-256 校验和。

### 原子提交顺序

1. 从全量去重事件重建案件。
2. 写案件快照临时文件，`fsync`，原子替换 `cases.json`。
3. 写检查点临时文件，`fsync`，原子替换 `checkpoint.json`。
4. 对目录执行 `fsync`（非 Windows）。

临时文件名形如 `.checkpoint.json.*.tmp`，永远不会被加载为有效状态。

如果进程在步骤 2 后、步骤 3 前被强杀，磁盘上是“旧检查点 + 新代次案件快照”。加载器发现代次/校验和与检查点引用不一致，会忽略快照并使用旧检查点事件重建案件。如果进程在步骤 3 后被杀，则新检查点与对应快照均完整，恢复到新边界。

### 版本变化

- `schema_version` 不同：`E_SCHEMA_VERSION_CHANGED`，不使用旧派生状态，从检查点事件整体重建。
- 规则版本或指纹不同：`E_RULE_VERSION_CHANGED`，丢弃旧案件，使用新规则重建。
- 快照过期或损坏：`E_CASE_SNAPSHOT_STALE` / `E_CASE_SNAPSHOT_CORRUPT`，忽略并重建。
- `check-checkpoint` 还会从检查点事件确定性重算全部案件；即使快照元数据与校验和彼此自洽，只要案件内容不一致也会报告 `E_CASES_MISMATCH`。
- `check-checkpoint --repair` 会把重建结果原子提交为新的完整代次。

### 并发

所有读写状态的 CLI 命令在操作期间持有工作目录文件锁：

- POSIX：`fcntl.flock(..., LOCK_EX | LOCK_NB)`
- Windows：`msvcrt.locking(..., LK_NBLCK, 1)`

锁已被占用时立即返回 `E_LOCK`，不会等待，也不会修改任何状态。

## 业务错误码

| 分类 | 错误码 |
| --- | --- |
| 行解析 | `E_ENCODING`, `E_EMPTY_LINE`, `E_JSON`, `E_NOT_OBJECT` |
| 字段校验 | `E_MISSING_FIELD`, `E_INVALID_TYPE`, `E_INVALID_VALUE`, `E_TS_RANGE`, `E_SEQ_RANGE` |
| 投递与序列 | `E_DUPLICATE_EVENT`, `E_EVENT_CONFLICT`, `E_SEQ_REGRESSION`, `E_SEQ_GAP` |
| 因果 | `E_ORPHAN`, `E_CYCLE`, `E_CROSS_TRACE` |
| 规则 | `E_RULES`, `E_RULE_VERSION_CHANGED`, `E_CHECKPOINT_RULES` |
| 状态与恢复 | `E_NO_CHECKPOINT`, `E_CHECKPOINT_CORRUPT`, `E_CASE_SNAPSHOT_CORRUPT`, `E_CASE_SNAPSHOT_STALE`, `E_SCHEMA_VERSION_CHANGED`, `E_CHECKPOINT_REBUILD_REQUIRED`, `E_CASES_NOT_CURRENT`, `E_CASES_MISMATCH` |
| 并发与 IO | `E_LOCK`, `E_WORKSPACE`, `E_WORKSPACE_WRITE`, `E_INPUT`, `E_IO`, `E_REPORT_WRITE` |
| CLI | `E_ARGS`, `E_CASE_NOT_FOUND` |

error 级记录会造成 `partial`/退出码 1；warning 级记录写入报告但不单独使成功路径失败。业务码始终位于 JSON 的 `errors`、`rejects`、`anomalies` 或 `load.issues` 中，不会只输出纯文本。

## 确定性样本与一键演示

`scripts/generate_samples.py` 生成：

- `events_ordered.jsonl`：正常顺序、无异常事件。
- `events_shuffled.jsonl`：同一事件集合的固定乱序。
- `events_duplicates.jsonl`：固定乱序加两次完全重复投递。
- `events_anomalies.jsonl`：孤儿、自环、长环、跨 trace、重复 seq、seq 空洞、冲突投递、完全重复、畸形行。
- `events_empty.jsonl`。
- 默认规则与短时间窗规则。
- 记录各文件 SHA-256 的 `manifest.json`。

一键演示：

```powershell
python scripts\run_demo.py --output-dir .\demo-run
```

演示脚本会重新生成样本，并验证：

1. 顺序、乱序、重复三路导入的案件键与成员一致。
2. 异常样本产生拒绝与所有关键异常码。
3. 规则版本变化后案件从 3 个重建为 0 个。
4. 子进程在检查点替换前强制退出，恢复代次仍为最后一个完整检查点；修复后可继续导入。

## 实际验证记录

本次实现实际运行环境为 Linux 工作容器，Python `3.11.15`，pytest `9.2.0`。Windows 分支使用标准库 `msvcrt` 与 `os.replace`，但本次环境未执行真实 Windows 进程测试。

```text
$ .venv/bin/python -m pytest -q
.......................                                     [100%]
23 passed
```

覆盖点包括：

- 逐行畸形输入继续处理、错误码与行号。
- 孤儿、自环、长环、跨 trace 父引用。
- 重复 seq、seq 空洞、完全重复、冲突首次值保留。
- 4 个事件的 24 种到达排列案件身份完全一致。
- 每事件检查点下顺序与乱序最终案件一致。
- 规则版本变化重建、结构版本变化重建、过期快照忽略、自洽但语义错误的快照被识别。
- 强制终止后仅恢复到最后一个完整检查点，过期快照重建后再修复。
- 并发锁冲突返回 `E_LOCK` 且不创建状态。
- 四类 CLI 子命令、JSON 报告、退出码 0/1/2、案件查询与完整导出。

一键演示实际输出摘要：

```text
ordered/shuffled/duplicates: 3 个案件，案件键与成员全部一致
anomaly import: partial，包含 E_ORPHAN/E_CYCLE/E_CROSS_TRACE/E_SEQ_GAP/E_EVENT_CONFLICT
rule rebuild: default-v1 3 cases -> short-window-v1 0 cases
forced termination: crash exit 137 -> recovered generation 1 -> repair -> resume -> 9 unique events
```

## 设计边界

- 检查点保存完整去重事件快照，而不是无限增长的仅追加事件日志；这使演示、审计与确定性重建简单可靠。超大历史数据集可在此基础上增加分段日志与压缩。
- 案件聚合采用确定性连通分量，不依赖系统时钟、哈希随机种子或输入到达顺序。
- 所有测试数据与样本均由本地脚本生成，未访问网络或外部目标。
