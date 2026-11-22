# 本地块存储快照与回滚组件

这是一个仅依赖 Python 标准库的本地快照组件（测试依赖 `pytest`/`numpy`），用于在确定性样本上验证：

- 块级写时复制（COW）、内容寻址存储和摘要去重；
- 带分叉的快照父子链、任意存活快照回滚；
- 回滚后的分支失效闭包、孤儿标记与零引用块 GC；
- SQLite 事务原子提交、文件锁、多进程并发与幂等；
- 快照创建、回滚切换和 GC 任一阶段强制终止后的恢复。

运行时代码在 `blockstore/`，样本在 `samples/`，自动化测试在 `tests/`，可重复演示在 `scripts/demo.py`。

## 环境准备

当前实测环境：

```text
Python 3.11.18
SQLite 3.53.1（Python 标准库 sqlite3）
numpy 2.4.6
pytest 9.2.0
```

使用 `uv` 创建 Python 3.11 虚拟环境：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate                 # Linux/macOS
# Windows: .venv\Scripts\activate
uv pip install pytest numpy
python -m pytest -q
```

运行时不需要安装第三方包。Windows 下统一使用模块入口：

```powershell
python -m blockstore --root data status
```

## 样本格式

### 卷描述

`samples/volume.json`：

```json
{
  "volume_id": "vol-demo",
  "block_size": 8,
  "block_count": 8,
  "created_clock": 0,
  "blocks": [
    {
      "block_no": 0,
      "data": "BASE0000",
      "encoding": "utf8",
      "sha256": "87babdd87b650d2dc0487cc03d1d989515200de093910b5d768e14dd4aee5aa0"
    }
  ]
}
```

- `blocks` 必须恰好覆盖 `0..block_count-1`，每块解码后长度必须等于 `block_size`。
- `encoding` 支持 `utf8`、`hex`、`base64`。
- `sha256` 可省略；提供时必须与内容摘要一致。
- 每个数据目录只加载一个卷。卷的初始状态在内部登记为保留快照 `"{volume_id}@base"`，因此快照父 ID 可以安全引用基线。

### 快照计划

`samples/snapshots.json`：

```json
{
  "volume_id": "vol-demo",
  "snapshots": [
    {
      "snapshot_id": "snap-a1",
      "seq": 1,
      "parent_snapshot_id": "vol-demo@base",
      "created_clock": 1000,
      "covered_start": 0,
      "covered_end": 1,
      "blocks": [
        {
          "block_no": 0,
          "data": "A1000000",
          "encoding": "utf8",
          "sha256": "8c05d20f0c2b72b285bdffd4e100468417a2059ca9ed2cd63ea799a45f55c3a8"
        }
      ]
    }
  ]
}
```

- `snapshot_id`、`seq` 唯一；`seq` 必须为正整数。
- 父快照必须存在，且 `created_clock` 严格大于父快照时钟。
- `covered_start..covered_end` 为闭区间，必须在卷范围内；`blocks` 必须逐块覆盖该区间且摘要与内容一致。
- 也接受非连续语义下的 `covered_blocks: [...]`，但当前要求它本身连续。
- 计划可以按任意 JSON 顺序排列；加载时按 `(created_clock, seq, snapshot_id)` 的稳定顺序拓扑提交。
- 未覆盖块沿用父快照的块归属，`snapshot_blocks.source` 记录为 `inherit`，覆盖块记录为 `cow`。

### 回滚请求

`samples/rollbacks.json`：

```json
{
  "volume_id": "vol-demo",
  "rollback_requests": [
    {
      "request_id": "rb-main",
      "target_snapshot_id": "snap-a2",
      "rollback_clock": 10000,
      "keep_after_target": false
    }
  ]
}
```

- 目标快照必须存活。
- `rollback_clock` 必须严格大于目标快照时钟，也必须严格大于当前已提交链视图的逻辑时钟水位。
- 同一 `request_id` 重复提交且载荷相同返回幂等成功；载荷不同返回 `REQUEST_CONFLICT`。
- 无论 `keep_after_target` 取值，冲突分支都必须失效；该字段作为审计保留意图持久化在回滚结果中。

## CLI

所有命令输出稳定排序 JSON。默认缩进输出，`--compact` 输出紧凑 JSON。业务错误也输出 JSON，并以退出码 `2` 结束。

```bash
# 1. 校验完整输入、初始化基线并应用快照计划
python -m blockstore --root data load \
  --volume samples/volume.json \
  --snapshots samples/snapshots.json \
  --rollbacks samples/rollbacks.json

# 只初始化基线，不应用快照计划
python -m blockstore --root data load --volume samples/volume.json --base-only

# 从计划文件中创建指定快照
python -m blockstore --root data snapshot \
  --file samples/snapshots.json --snapshot-id snap-a1

# 执行回滚
python -m blockstore --root data rollback \
  --file samples/rollbacks.json --request-id rb-main

# 或直接传参
python -m blockstore --root data rollback \
  --request-id rb-direct --target snap-a2 --clock 10000

# 查看链视图
python -m blockstore --root data status

# 删除零引用块
python -m blockstore --root data gc

# 完成 prepared/deleting 阶段的中断操作
python -m blockstore --root data resume

# 恢复中断操作后校验链视图、块归属和引用计数
python -m blockstore --root data verify
```

`status` 包含当前 `view_version`、逻辑时钟水位、卷锚点快照、每个快照的 `live/orphan` 状态、孤儿触发来源与引用路径、待恢复操作数量，以及按摘要排序的块引用计数摘要。

`verify` 校验：

- 快照 manifest、父链和时钟；
- 每个快照的完整块归属映射；
- 从父链重放出的可见映射与 `snapshot_blocks` 一致；
- 当前卷映射与锚点快照一致；
- `block_refs`、`block_counts` 与卷/存活快照/未提交事务引用一致；
- 所有活跃摘要文件存在且 SHA-256 未变；
- 没有遗留 prepared/deleting 操作和事务引用。

## 回滚与失效规则

回滚到目标 `T` 时：

1. 保护 `T` 及其全部祖先，保持 `live`。
2. 对所有 `created_clock > T.created_clock` 且不在 `T` 祖先链上的存活快照计算失效闭包。
3. 将这些快照标记为 `orphan`，记录 `orphaned_by`、原因、裁决时视图版本，以及 `target_to_lca`、`orphan_to_lca` 引用路径。
4. 删除孤儿快照的活跃块引用，使其不再阻止 GC。
5. 卷的 `volume_blocks` 整体切换为 `T` 的可见映射，`current_view` 版本原子递增。

样本 `snap-a1 -> snap-a2 -> snap-a3` 与 `base -> snap-b1 -> snap-c1` 在回滚到 `snap-a2` 后，`snap-b1`、`snap-a3`、`snap-c1` 都会成为孤儿；`snap-a1`、`snap-a2` 和基线保持存活。

并发写操作由跨平台文件锁串行化，并由 SQLite `BEGIN IMMEDIATE` 提交。并发回滚采用严格逻辑时钟裁决：时钟更大的请求可以覆盖较小者；同一时钟仅第一个提交者获胜，其余请求返回 `ROLLBACK_STALE`/`ROLLBACK_CONFLICT`。相同 `request_id` 不会重复递增视图版本。

## 持久化与恢复设计

状态目录结构：

```text
data/
  .lock
  state.sqlite3
  state.sqlite3-wal
  state.sqlite3-shm
  blocks/
    <sha256>.blk
    .tmp-<sha256>-<pid>-<uuid>
```

块文件写入流程为“唯一临时文件 + `fsync` + `os.replace` + 目录 `fsync`”。文件名就是摘要，相同内容重复写入复用同一文件。若已存在文件摘要不符，操作以 `BLOCK_CORRUPT` 拒绝使用。

SQLite 使用 WAL、`synchronous=FULL`、`foreign_keys=ON` 和 30 秒 busy timeout。关键表：

- `snapshots`：父链、状态、manifest、视图版本、孤儿审计；
- `snapshot_blocks`：每个快照的完整块号到摘要映射及 `base/cow/inherit` 来源；
- `volume_blocks`：当前卷视图；
- `blocks` / `block_refs` / `block_counts`：摘要元数据、引用来源和引用计数；
- `transaction_refs`：prepare 后但尚未提交的快照事务引用；
- `snapshot_runs` / `rollback_requests` / `gc_runs` / `gc_actions`：阶段化和可恢复操作日志。

### 故障注入点

通过环境变量 `BLOCKSTORE_CRASH_POINT` 在以下阶段调用 `os._exit(97)`：

- `snapshot:prepared`、`snapshot:materialized`、`snapshot:committed`
- `rollback:prepared`、`rollback:committed`
- `gc:planned`、`gc:deleting`、`gc:after_one_delete`、`gc:deleted`

恢复顺序为：清理临时块文件 → 完成 prepared 快照 → 完成 prepared 回滚 → 完成 prepared/deleting GC。每一步都在独立 SQLite 事务中幂等执行。回滚在提交前不会改变卷视图；读者只能看到已提交的完整链视图。快照 prepare 会预先把事务引用写入 `transaction_refs`，因此即使进入 materialize 阶段，GC 也不会把其摘要当作零引用块。

## 实际验证结果

### 自动化测试

执行：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
.........................                                                [100%]
25 passed
```

测试覆盖：

- 非法摘要、重复快照序号、时钟回退、覆盖不一致、无持久化落盘；
- 已有状态遇到卷初始指纹不匹配时视图完全不变；
- COW 继承、同摘要去重、manifest/父链重放、篡改拒绝；
- 回滚孤儿闭包、卷块映射切换、幂等、同钟冲突；
- GC 精确删除 5 个零引用块并保留 11 个引用块、重复 GC 报告一致；
- 快照、回滚、GC 三类强杀恢复；
- prepared 快照的事务块引用持久化，恢复后清零；
- 4 进程同请求回滚只提交一次、3 进程 GC 返回同一报告、同序号并发快照仅一个提交；
- 多进程并发冲突裁决和恢复后 `verify`。

### 可重复演示

执行：

```bash
.venv/bin/python scripts/demo.py
```

本次实测输出：

```json
{
  "normal_flow": {
    "gc_deleted_blocks": 5,
    "gc_retained_blocks": 11,
    "live_snapshots": 4,
    "loaded_view_version": 6,
    "orphan_snapshots": 3,
    "orphaned_snapshots": ["snap-a3", "snap-b1", "snap-c1"],
    "post_rollback_snapshot": "snap-a4",
    "rollback_view_version": 7,
    "verify_ok": true
  },
  "recovery_flow": {
    "anchor_before_resume": "vol-demo@base",
    "fault_exit_code": 97,
    "load_view_version": 6,
    "pending_rollbacks_before_resume": 1,
    "recovery_action": "complete_rollback",
    "verify_ok": true,
    "view_after_resume": 7,
    "view_before_resume": 6
  }
}
```

### 中断恢复实测

| 故障点 | 中断后可见状态 | 恢复结果 |
| --- | --- | --- |
| `snapshot:materialized` | 视图 1，仅有基线，pending snapshot=1 | `resume` 完成视图 2，`snap-a1` 出现，`verify.ok=true` |
| `rollback:prepared` | 视图 6，锚点仍为基线，pending rollback=1 | `resume` 完成视图 7，锚点 `snap-a2`，3 个孤儿，`verify.ok=true` |
| `gc:after_one_delete` | 视图 7，pending gc=1，活跃引用仍为 11 个摘要 | `resume` 完成 GC，删除 5、保留 11，重复 GC 幂等，`verify.ok=true` |

## 业务错误码

错误统一以如下形式输出：

```json
{
  "ok": false,
  "error": {
    "code": "DIGEST_MISMATCH",
    "message": "block digest does not match its content",
    "details": {}
  }
}
```

主要错误码包括 `INVALID_JSON`、`DUPLICATE_SNAPSHOT_ID`、`DUPLICATE_SNAPSHOT_SEQ`、`PARENT_NOT_FOUND`、`PARENT_ORPHANED`、`CLOCK_NOT_MONOTONIC`、`DIGEST_MISMATCH`、`COVERAGE_MISMATCH`、`ROLLBACK_STALE`、`ROLLBACK_CONFLICT`、`ROLLBACK_TARGET_ORPHANED`、`REQUEST_CONFLICT`、`SNAPSHOT_CONFLICT`、`BLOCK_MISSING`、`BLOCK_CORRUPT`、`VERIFY_FAILED`、`STORAGE_NOT_INITIALIZED`。

## 设计边界

- 每个状态目录只承载一个卷；卷 ID 在目录内唯一。
- 所有写操作通过全局文件锁串行化，读者依赖 SQLite WAL 读取完整提交视图；这优先保证确定性、去重和跨进程恢复正确性。
- 内容寻址只对块内容去重，不做压缩。
- GC 以链视图版本为单位绑定；同一视图重复 GC 返回持久化的同一份回收清单。
