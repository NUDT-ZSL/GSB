# 本地块存储快照与回滚组件

这是一个零外部服务、确定性的本地块存储快照组件。它把卷、快照写时复制块和
快照链元数据物化为本地文件与 SQLite 数据库，支持快照创建、任意存活快照回滚、
分支失效、块级引用计数、GC、强制终止恢复以及多进程幂等提交。

## 快速开始

要求 Python 3.11 及以上，SQLite 与 `hashlib` 为标准库。组件不依赖 NumPy。

```bash
python3.11 -m blockstore load \
  --state artifacts/demo_state \
  --sample samples/fixed_sample.json

python3.11 -m blockstore snapshot --state artifacts/demo_state --sequence 1
python3.11 -m blockstore snapshot --state artifacts/demo_state --sequence 2
python3.11 -m blockstore snapshot --state artifacts/demo_state --sequence 3
python3.11 -m blockstore rollback --state artifacts/demo_state --request-id rb-001
python3.11 -m blockstore status --state artifacts/demo_state
python3.11 -m blockstore verify --state artifacts/demo_state
```

完整双跑演示：

```bash
python3.11 scripts/run_demo.py
python3.11 -m unittest discover -v
```

演示报告写入 `artifacts/reproducibility_report.json`。实际验证结果见
`docs/VERIFICATION.md`。

## 目录结构

```text
blockstore/
  model.py      样本解析、校验、摘要与可见视图哈希
  storage.py    内容寻址块文件、临时文件+重命名、文件锁
  db.py         SQLite 模式与事务辅助
  engine.py     快照、回滚、引用、GC、恢复、status、verify
  cli.py        命令行接口
samples/
  fixed_sample.json
scripts/
  run_demo.py
tests/
docs/
  VERIFICATION.md
```

运行时状态目录布局：

```text
<state>/
  meta.sqlite3
  blocks/<sha256>
  tmp/
  trash/
  locks/blocks/<sha256>.lock
  store.lock
```

## 样本格式

块内容使用 `data_base64` 提供，`digest` 必须是该内容的 SHA-256。这样样本既
能明确给出内容摘要，也能实际生成内容寻址块文件。

```json
{
  "format_version": 1,
  "volume": {
    "id": "vol-demo",
    "block_size": 16,
    "block_count": 4,
    "base_snapshot_id": "snap-base",
    "initial_clock_ms": 0,
    "blocks": [
      {
        "block": 0,
        "data_base64": "...",
        "digest": "<sha256>"
      }
    ]
  },
  "snapshots": [
    {
      "sequence": 1,
      "id": "snap-001",
      "parent_id": "snap-base",
      "clock_ms": 1000,
      "ranges": [
        {
          "start": 0,
          "end": 0,
          "blocks": [
            {"block": 0, "data_base64": "...", "digest": "<sha256>"}
          ]
        }
      ]
    }
  ],
  "rollbacks": [
    {
      "request_id": "rb-001",
      "target_snapshot_id": "snap-001",
      "clock_ms": 4000,
      "retain_after": true
    }
  ]
}
```

校验规则包括：

- 卷 ID、块大小、块数量、初始块集合完整且合法。
- 快照 ID 与序号唯一；父快照存在；快照时钟严格晚于父快照。
- 每个 `ranges` 项完整、按序覆盖 `[start, end]`，且不与其他区间重叠。
- 块号在卷范围内，块内容长度等于 `block_size`，摘要等于实际内容 SHA-256。
- 回滚请求 ID 唯一，目标快照在样本中存在。
- 所有校验在创建状态目录之前完成。失败返回 `{"ok":false,"error":{"code":...}}`。

## 状态模型

`snap-base` 是表示卷初始内容的根快照。每次 `snapshot`：

1. 按计划从父快照继承块级 `(digest, owner_snapshot_id)` 视图。
2. 只物化被该快照覆盖的写时复制块。
3. 在单个 SQLite 事务中写入快照、覆盖区间、每块摘要、完整可见块视图、
   卷当前视图、引用来源表、链版本与时钟水位。
4. 将当前卷切换到新快照。

因此任意快照的可见内容既可由 `snapshot_writes + parent_id` 沿链回溯计算，
也可与持久化的完整 `snapshot_blocks` 视图交叉校验。

### 回滚与分叉

回滚目标必须是存活快照。回滚后：

- 卷块视图整体切换为目标快照的完整可见视图。
- 当前快照指针、链版本、逻辑时钟水位在同一事务中更新。
- 候选失效集合定义为：存活、非目标祖先、非根快照，且创建时钟晚于目标。
- `retain_after=true`：候选成为保留侧分支，引用继续有效。
- `retain_after=false`：候选标记为 `orphan`，写入失效原因，并从
  `block_refs` 删除其快照引用；元数据保留供审计。
- 每个候选的 `preserved`/`orphaned` 裁决写入 `fork_decisions`，并出现在
  `rollback` 输出与 `status.rollback_history` 中。

保留侧分支后继续创建快照时，新快照以当前快照为父节点，自然形成分叉。

## 引用与 GC

`block_refs` 是引用计数真值表，来源类型为：

- `volume`：当前卷可见视图。
- `snapshot`：每个存活快照的完整可见块视图。
- `transaction`：尚未最终提交的快照或回滚操作引用。

引用计数按 `(source_kind, source_id, block_index)` 聚合。孤儿快照不产生引用。
GC 只删除没有任何引用的块文件。每个 GC run 绑定开始时的链视图版本，并在
`gc_runs`、`gc_candidates`、`gc_items` 中记录扫描数、回收数、保留数、摘要和
保留原因。

`status.gc.reclaimed_blocks_total` 给出累计回收数；
`status.reference_counts[*].sources` 与 `status.gc.last_run.items` 给出保留原因。

## 原子性与崩溃恢复

块文件是不可变的。写入顺序为：

1. 在 `<state>/tmp` 写临时文件并 `fsync`。
2. 在相同文件系统内 `os.replace` 到 `blocks/<digest>`。
3. `fsync` 块目录。

每个摘要还有一个 `fcntl.flock` 文件锁，因此多个进程不会重复物化同一摘要。
所有写命令另持有状态目录级写锁；读命令不持有该锁。SQLite 使用 WAL、
`busy_timeout` 和显式 `BEGIN IMMEDIATE`。

快照/回滚先提交一条 `operations` pending 记录及 `transaction` 块引用，再落块，
最后用一个 SQLite 事务提交完整状态并删除事务引用。回滚的卷切换、孤儿裁决、
引用删除、时钟水位和请求结果都在同一个事务内，不会出现“卷已回滚但元数据仍
指向旧分支”的已提交中间态。

GC 先提交候选计划，再隔离并删除文件，最后提交完成记录。恢复逻辑：

- pending 快照/回滚：删除事务引用，标记 `aborted_on_resume`，保持旧链视图。
- running GC：基于持久化候选集补完删除并标记完成。
- 清理 `tmp` 与 `trash` 中无引用临时/隔离文件。
- `resume` 报告实际处理结果，并列出仍等待 GC 的零引用最终块。

强制终止后不承诺原进程还能生成报告；只能保证下一次 `resume`/写命令依据已提交
状态恢复一致性。故障注入点通过环境变量 `BLOCKSTORE_CRASH_AT` 设置，例如：

- `snapshot:after_pending`
- `snapshot:after_blocks`
- `snapshot:before_final_commit`
- `rollback:before_final_commit`
- `gc:after_plan`
- `gc:mid_delete`
- `gc:before_finalize`

## CLI

```text
blockstore load     --state DIR --sample FILE
blockstore snapshot --state DIR --sequence N
blockstore rollback --state DIR --request-id ID
blockstore gc       --state DIR
blockstore resume   --state DIR
blockstore status   --state DIR
blockstore verify   --state DIR
```

所有输出为稳定排序 JSON。`status` 包含链版本、时钟水位、当前快照、每个快照的
`alive`/`orphan` 状态与分叉原因、卷块归属、引用来源、块对象统计、GC 记录和待
恢复事务。`verify` 会检查 SQLite 完整性、父链与块视图回溯、卷当前视图、引用
来源表、被引用块文件摘要、孤儿引用和临时文件，并返回 `checks/errors/warnings`。

## 错误码

常见业务错误码包括：

```text
INVALID_JSON, INVALID_SCHEMA, INVALID_BLOCK_SIZE, INVALID_BLOCK_COUNT,
INVALID_BLOCK_DATA, INVALID_DIGEST, DIGEST_MISMATCH, DUPLICATE_BLOCK,
MISSING_BLOCK, DUPLICATE_SNAPSHOT_ID, DUPLICATE_SNAPSHOT_SEQUENCE,
INVALID_SNAPSHOT_SEQUENCE, PARENT_NOT_FOUND, CLOCK_NOT_MONOTONIC,
BLOCK_RANGE_OUT_OF_BOUNDS, INVALID_RANGE_COVERAGE, DUPLICATE_WRITE_BLOCK,
DUPLICATE_ROLLBACK_ID, ROLLBACK_TARGET_NOT_FOUND, SNAPSHOT_NOT_ALIVE,
PARENT_NOT_ALIVE, CLOCK_WATERMARK_REGRESSION, STATE_NOT_FOUND,
STATE_CONFLICT, STATE_CORRUPT, SNAPSHOT_ID_CONFLICT,
SNAPSHOT_SEQUENCE_CONFLICT, CONCURRENT_CONFLICT, VERIFY_FAILED
```

非法 `load` 输入在目录创建前失败；已有状态不会因解析错误被修改。

## 限制与说明

- 样本和命令均使用逻辑时钟，不读取系统时间。
- 写事务通过状态目录级文件锁串行化；多个进程可以同时发起请求，读方通过 WAL
  读取完整已提交版本。逻辑顺序冲突（例如父快照尚未创建）会返回明确错误码。
- 不依赖 Docker、网络、外部数据库或未授权目标。
- `verify` 对崩溃后遗留的零引用块给出 warning，`gc` 会安全回收它们。
