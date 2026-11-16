# SQLite 事务隔离与行锁等待实验台

面向 Windows 本地环境、零第三方依赖的领域数据编辑实验台。应用使用本地 SQLite 文件保存已提交数据，使用 Python 应用层行锁管理器管理共享锁/排他锁、稳定 FIFO 等待队列、等待预算、死锁检测与回滚仲裁，并提供确定性调度轨迹、命名快照、整体替换恢复和重放校验。

## 目录

- `main.py`：统一命令行入口。
- `txlab/`：脚本校验、SQLite 存储、行锁、调度器、快照、重放、CLI 与 Tkinter 界面。
- `samples/`：初始行 SQL 和 5 个确定性编辑脚本。
- `tests/`：标准库 `unittest` 测试，共 19 项。
- `docs/DESIGN.md`：调度、锁语义、事件和快照格式说明。
- `reports/verification.txt`：本次实际验证记录。
- `runtime/`、`snapshots/`、`reports/demo_report.json`：运行时生成物，已加入 `.gitignore`。

## Windows 环境准备

要求 Python 3.10 及以上。Windows 官方 Python 安装包默认包含 `sqlite3` 与 `tkinter`。

```bat
cd /d C:\path\to\uid21
check_env.bat
```

等价的预检命令：

```bat
py -3 -c "import sys, sqlite3, tkinter; print(sys.version); print('SQLite', sqlite3.sqlite_version); print('Tk', tkinter.TkVersion)"
```

若 `tkinter` 不可用，重新运行 Python 安装程序，选择 **Modify**，勾选 **tcl/tk and IDLE**，或安装包含 Tk 的官方 Python。也可用 `py -m tkinter` 打开 Tk 测试窗口确认。

不依赖 Docker、数据库服务、网络或第三方 Python 包。

## 快速开始

```bat
run_demo.bat
run_gui.bat
run_tests.bat
```

命令行等价方式：

```bat
python main.py demo
python main.py gui
python -m unittest discover -s tests -v
```

单独执行一个脚本：

```bat
python main.py run ^
  --script samples\deadlock.json ^
  --init-sql samples\initial_rows.sql ^
  --db runtime\manual.sqlite3 ^
  --budget 500 ^
  --concurrency 2
```

保存快照、恢复与重放：

```bat
python main.py run --script samples\normal_parallel.json --init-sql samples\initial_rows.sql --db runtime\lab.sqlite3 --budget 500 --concurrency 2 --snapshot normal-v1
python main.py snapshots
python main.py replay --snapshot snapshots\normal-v1.json
python main.py restore --snapshot snapshots\normal-v1.json --db runtime\lab.sqlite3
```

## 内置确定性场景

`python main.py demo` 固定按以下顺序执行：

1. `normal_parallel.json`：3 个会话，并发上限 2；全部提交，版本 `0 -> 3`，摘要一致。
2. `lock_timeout.json`：`t1` 持有 `account:A` 写锁，`t2` 等待超过 40ms 后以 `E_LOCKTIMEOUT` 回滚，写入不落地。
3. `deadlock.json`：`d1` 与 `d2` 形成等待环；按 `lowest_session_id` 规则稳定牺牲 `d1`，`d2` 提交。
4. `isolation_rc.json`：READ COMMITTED 读会话两次读到 `100 -> 70`，体现不可重复读。
5. `isolation_rr.json`：REPEATABLE READ 读会话两次都读到 `100`，第二次来自事务读缓存。

演示还会执行三组重放校验：

- 正常场景同参数重放：`OK`，事件序列逐项一致。
- 死锁场景同参数重放：`OK`，牺牲事务仍为 `d1`。
- 超时场景把等待预算改为 500ms：稳定报告 `E_REPLAY`，分歧会话为 `["t2"]`。

## 编辑脚本格式

脚本为 UTF-8 JSON。每个会话声明隔离级别和有序操作列表，最后一个操作必须是 `commit` 或 `rollback`。

```json
{
  "script_version": 1,
  "name": "example",
  "sessions": [
    {
      "id": "s1",
      "isolation": "repeatable_read",
      "operations": [
        {"op": "read", "key": "account:A"},
        {"op": "write", "key": "account:A", "value": {"balance": 90}},
        {"op": "commit"}
      ]
    }
  ]
}
```

校验在调度开始前整体完成。以下情况返回 `E_SCRIPT`，并尽量给出 `session_id` 与 `op_index`：

- 根对象、会话或操作结构非法；
- 隔离级别不是 `read_committed` / `repeatable_read`；
- 会话 ID 重复；
- 读/写目标键不存在；
- 写操作缺少 `value`；
- `commit`/`rollback` 后仍有操作；
- 会话未以 `commit`/`rollback` 结束。

非法脚本不会执行任何事务，已经开始的数据库重置也只停留在确定性初始状态。

## 核心机制

### SQLite 与版本

- 每个运行使用一个本地 SQLite 文件，应用表为 `lab_rows` 与 `lab_meta`。
- 事务写入先暂存在会话运行时；`commit` 时一次 SQLite 事务原子落地。
- 全局 `data_version` 只在有写入的提交发生时加 1，行版本同步更新。
- 运行结束时按提交串行化顺序重算行内容摘要，并与 SQLite 实际摘要比较；不一致报告 `E_ISOLATION`。

### 行锁与隔离

- `read` 申请共享锁 `S`，`write` 申请排他锁 `X`，粒度为行键。
- 等待队列为稳定 FIFO；新申请不能绕过队首等待者；S→X 升级也进入等待图。
- 排他锁和 REPEATABLE READ 的共享锁持有至事务结束。
- READ COMMITTED 的共享读锁是语句锁，读语句结束即释放，从而允许其他事务提交并产生真实的不可重复读；这是实现其隔离语义所必需的语句级例外。
- REPEATABLE READ 首次读取后缓存可见值，同一事务后续读取返回相同值。
- 超时或死锁回滚会释放该事务持有的全部锁与全部等待项。

### 确定性调度

- 全局并发上限决定同时处于 running/waiting 的会话数。
- 调度使用逻辑时钟和固定 `tick_ms`；事件同时记录逻辑 `ts_ms` 和实际 UTC `timestamp`。
- 每次调度量子推进一个会话的一步；等待者仍占用槽位，直到获得锁、超时或被死锁仲裁回滚。
- 同一脚本、参数、初始数据在同一实现版本下产生相同的事务结局、逻辑时间和事件序列。

### 死锁仲裁

- 根据等待者到锁持有者/队首等待者的关系构造等待图。
- 使用强连通分量检测等待环。
- 仲裁规则固定为 `lowest_session_id`：环成员中会话 ID 字典序最小者成为牺牲事务。
- 结构化 `deadlock_detected` 日志包含规则、完整环成员、牺牲事务和等待图。

### 快照与恢复

命名快照以原子 JSON 写入，包含：

- 完整脚本与各会话隔离级别；
- 等待预算、并发上限、调度 tick；
- 完整执行轨迹与事务结局；
- 初始行状态、最终行状态、摘要与结果数据版本；
- 快照格式版本与状态版本。

恢复不进行行级合并。无论当前数据库版本是否相同，都先清空应用表，再一次性写入快照中的完整行状态与版本；恢复报告包含 `restore_mode=full_replace`、恢复前后版本、`version_mismatch` 和摘要校验结果。强制终止进程后，恢复只依赖已原子落盘的快照文件。

### 重放校验

重放使用快照中的初始行状态、脚本和参数，在独立 SQLite 文件中重新调度。比较内容包括：

- 每个会话的 `commit` / `rollback` 结局与业务码；
- 规范化事件序列（类型、顺序、逻辑时间、会话、行键、锁模式、值、死锁信息等；排除实际 UTC 写入时间）。

同参数重放应完全一致。改变等待预算或并发上限若导致结局/事件变化，返回 `E_REPLAY`，并按脚本会话顺序稳定列出分歧事务。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_SCRIPT` | 脚本整体校验失败，附会话与操作下标（适用时） |
| `E_LOCKTIMEOUT` | 单次锁等待预算耗尽，事务回滚 |
| `E_DEADLOCK` | 事务被死锁仲裁选为牺牲者，事务回滚 |
| `E_ISOLATION` | 提交后数据摘要/版本与串行化重算结果不一致 |
| `E_REPLAY` | 快照不兼容或重放结局/事件序列不可复现 |
| `E_STORAGE` | 本地文件、SQLite 或运行环境错误 |

## Tkinter 界面

`python main.py gui` 或 `run_gui.bat` 打开桌面界面。界面提供：

- 脚本、初始 SQL、SQLite 文件、快照目录选择；
- 等待预算、并发上限、调度 tick、可视化节流；
- 会话阶段、隔离级别、持有行锁、等待位置、已用等待预算、结局；
- 行锁持有者与 FIFO 等待队列；
- 实时执行轨迹；
- 执行脚本、保存快照、恢复快照、重放校验。

调度在后台线程运行，通过线程安全队列刷新 Tk 主线程，避免界面冻结。

## CLI 命令

```text
python main.py run        执行脚本
python main.py validate   仅校验脚本
python main.py demo       运行完整验收演示
python main.py snapshots  列出快照
python main.py restore    整体替换恢复
python main.py replay     重放校验
python main.py gui        启动桌面界面
```

完整参数见：

```bat
python main.py --help
python main.py run --help
```

## 本次实际验证结果

验证环境：Linux 验证容器，Python `3.11.0rc1`，SQLite `3.37.2`，当前容器未安装 Tkinter 且无可用 sudo 密码；Tkinter 源码已通过 `py_compile`，Windows 官方 Python 自带 Tk 可直接运行。

```text
$ python3 -m unittest discover -s tests -v
Ran 19 tests
OK

$ python3 main.py demo --quiet-events
PASS normal_all_commit
PASS timeout_rolls_back_with_code
PASS deadlock_cycle_and_victim
PASS read_committed_sees_latest_committed
PASS repeatable_read_stable
PASS normal_replay_identical
PASS deadlock_replay_identical_victim
PASS changed_budget_reports_e_replay
```

详细证据见 `reports/verification.txt`。
