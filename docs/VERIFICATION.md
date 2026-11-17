# 实际验证记录

验证日期：2026-11-17（Asia/Shanghai）

## 1. 执行环境

| 项目 | 实测值 |
| --- | --- |
| OS | Linux 6.18.33.2-microsoft-standard-WSL2 x86_64 |
| Python | 3.11.0rc1 |
| SQLite | 3.37.2 |
| Tkinter | 当前 Linux 容器未安装 |
| 第三方运行时依赖 | 无 |

Windows 是交付目标。官方 Windows 安装包包含 Tkinter；本容器缺少 Tkinter，因此
GUI 在本环境仅完成模块导入和语法验证，没有进行可视化窗口截图验证。CLI、
SQLite 持久化、快照、重放、并发和注入均已实际执行。

## 2. 自动化测试

命令：

```bash
python3 -m unittest discover -v
```

结果：`Ran 17 tests in 6.749s`，`OK`。

覆盖项：

1. 非法事务迁移返回 `E_TXN`，事务表与状态版本不变。
2. 同事务同键二次写入返回 `E_VERSION`，链位置为 `1`，版本表不变。
3. 链指针损坏识别为 `E_CHAIN`。
4. 事务固定快照重复判定字节一致；快照后提交不改变旧事务可见值。
5. 确定性样本 12 次读取全部快速/权威规范字节一致。
6. 快照恢复发生版本差异时整体替换，恢复后历史重放 14/14 项一致。
7. 快照缓存版本错位、链损坏、事务号回退分别返回 `E_CACHE`、`E_CHAIN`、`E_TXN`。
8. 人为篡改版本后的历史重放返回 `E_REPLAY`，包含键与事务号。
9. 模拟强制终止后，重启将未提交事务标记为 `rolled_back`，最新视图不可见。
10. 延迟聚合使用整数微秒定点，P95 最近秩和量化误差上界正确。
11. 超低延迟预算只记录降级原因，不改变快速查询规范结果。
12. 3 线程后台连续提交 8 次期间，所有交互查询快速/权威结果一致。
13. 无 `--budget-ms` 参数的 CLI 子命令（初始化、事务、历史、指标、快照）全部成功。
14. 缓存首次 miss、再次 hit；随后事务开始导致缓存条目整体清零并重新绑定状态版本。
15. 人为替换快速结果为不一致内容时，校验路径返回 `E_VISIBLE` 并定位首个分歧字段。
16. 独立 `samples/query_sequence.json` 的查询顺序、事务标签、表与键和可执行脚本逐项一致。
17. 使用伪 Tk 控件对象完成 GUI 初始化路径构建冒烟测试；真实窗口仍因容器无 Tkinter 未做可视验证。

## 3. 完整 CLI 验收

命令：

```bash
python3 -m mvcc_workbench --session-dir runs/acceptance demo \
  --output artifacts/verification.json
```

退出码：`0`。报告：`artifacts/verification.json`。摘要：

### 样本与重复性

- 确定性脚本：`deterministic-mvcc-baseline`
- 总步骤：`38`
- 可见性读取：`12`
- 快速/权威 canonical 字节一致：`12/12`
- 同事务、同键连续两次查询逐字节一致：`true`
- 重复查询 canonical SHA-256：`fe07e5ab1537d99221f8efc461447d0bf62bb5cb3f797970d007852da23959fe`

样本覆盖的关键结论：

- 事务读取自己的未提交写入；
- 活跃写事务的版本对观察者不可见；
- 在观察者取得快照后提交的事务仍不可见；
- 中间事务回滚后旧版本恢复可见；
- 已提交 tombstone 使键不可见；
- 回滚的复活写入不会使键重新出现；
- 只有回滚版本的新键没有可见值。

### 命名快照与整体替换

| 项目 | 结果 |
| --- | --- |
| 快照会话版本 | `27` |
| 恢复前当前版本 | `30` |
| 识别版本差异 | `true` |
| 恢复策略 | `whole_replacement` |
| 恢复后版本 | `27` |
| 恢复后 `next_txid` | `11` |
| 历史重放 | `14/14 matched` |
| 快照文件恢复前后字节 | 未改变 |

恢复前写入的 `post-snapshot` 键在恢复后不存在，证明没有增量合并。

### 输入异常注入

| 注入 | 预期 | 实际 | 状态版本保持不变 |
| --- | --- | --- | --- |
| `chain` | `E_CHAIN` | `E_CHAIN` | `true` |
| `cache` | `E_CACHE` | `E_CACHE` | `true` |
| `txn` | `E_TXN` | `E_TXN` | `true` |

链注入实际详情示例：`table=inventory, key=sku-1, version_id=5,
chain_position=1`。缓存注入详情：`cache_version=28, session_version=27`。
事务号回退详情：`next_txid=10, max_txid=10`。三项均写入
`runs/acceptance/logs/events.jsonl`。

单独用 CLI 注入并恢复也可复现：

```bash
python3 -m mvcc_workbench --session-dir runs/acceptance inject-snapshot \
  --source runs/acceptance/snapshots/baseline.mvccsnap.json \
  --output /tmp/chain-injected.json --kind chain
python3 -m mvcc_workbench --session-dir runs/acceptance restore-snapshot \
  --file /tmp/chain-injected.json
```

第二条命令退出码为 `2`，输出 `error.code = E_CHAIN`。

### 后台并发与一致视图

- 后台并发上限：`3`
- 提交任务：`8`
- 完成：`8`
- 错误：`0`
- 同时执行的快速/权威校验查询：`5`
- 所有查询 canonical 一致：`true`
- 查询内部快速与权威快照字段相同：测试中逐项断言通过

### 强制终止恢复

- 孤儿事务：`txid=1`
- 未提交版本已持久化，但进程未提交、未生成退出报告
- 重新打开会话后状态：`rolled_back`
- 最新视图可见：`false`
- 快速/权威一致：`true`
- 恢复日志明确记录 `exit_report_generated=false`

### 延迟观测

本报告聚合了 `19` 次快速路径查询：

- 均值：`12566 µs`
- 最近秩 P95：`78463 µs`
- 最大值：`78463 µs`
- 定点单位：整数微秒，scale = `1`
- 量化误差上界：`1 µs`
- 时钟基准：`time.perf_counter_ns (local monotonic counter)`
- 超预算记录：4 条 `query_degraded`，原因均为 `fast_path_budget_exceeded`

该容器中后台写线程、SQLite `synchronous=FULL` 与双路径校验共同造成部分高延迟；
这正好验证了超预算日志路径。所有超预算查询的可见性 canonical 仍与权威重放一致。
延迟数字只代表本机本次运行，不是跨机器性能承诺。

## 4. 复现命令

```bash
python3 -m compileall -q mvcc_workbench tests
python3 -m unittest discover -v
python3 -m mvcc_workbench --session-dir runs/acceptance demo \
  --output artifacts/verification.json
```

Windows PowerShell 对应命令：

```powershell
py -3 -m unittest discover -v
py -3 -m mvcc_workbench --session-dir runs\acceptance demo --output artifacts\verification.json
```

GUI：

```powershell
py -3 -m mvcc_workbench --session-dir run gui
```
