# Validation Report

验证日期：2026-11-20

## 环境

```text
Python 3.10.12
NumPy 1.26.4
pip 26.3
SQLite 3.37.2
multiprocessing start method: fork
不使用 Docker、数据库服务、GPU 或运行时网络
```

复现命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：

```text
Ran 10 tests in 11.891s
OK
```

覆盖项：

- 非法输入返回业务错误码且不创建状态库；
- plan 不写持久化状态；
- 相同 seed/描述的数据逐位一致，倾斜键集中；
- 重复 Shuffle 逐分区逐字节一致，行数守恒且键不跨分区；
- 三档规模分别选择广播、分片哈希、排序归并；
- 内连接、左外、右外在三种算法下均与真值一致；
- Shuffle 半写入和联接中途终止均可恢复；
- 并发同分区提交只产生一条提交记录；
- 版本变化整体替换当前结果指针；
- status 两次输出逐字节稳定。

## 算法裁决

配置内存上限为 `0.2 MiB`。广播上限为 `94,371` 字节，哈希分区上限为 `167,772`
字节。

```text
small  broadcast_hash
       broadcast_fit: left small-side estimated bytes 28275 <= broadcast limit 94371

medium shuffled_hash
       broadcast_rejected: left small-side estimated bytes 113659 > broadcast limit 94371;
       shuffled_fit: max partition estimated bytes 81556 <= hash limit 167772

large  sort_merge
       hash_rejected: max partition estimated bytes 266329 > hash limit 167772;
       sort_merge external spill path selected
```

## 单查询执行与真值

| Scale | Selected | Result rows | Truth rows | Mismatch | Peak partition rows | Spill bytes | Join logical ticks |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | broadcast_hash | 387 | 387 | 0 | 300 | 0 | 2.900 |
| medium | shuffled_hash | 5,677 | 5,677 | 0 | 598 | 0 | 2.400 |
| large | sort_merge | 53,315 | 53,315 | 0 | 1,944 | 941,353 | 83.554 |

三档最终结果摘要均与嵌套循环真值摘要一致：

```text
small  be0f94de00f8cfb22e0d796254170b708f67def37a4c0ccd4049ad2f4a5ce79f
medium 54cd5b54befba49dbbd990c66c0aa5c0032202db5e108d1f0bf154cfb6c3453c
large  d68ea3aef932c19cc111165d18d838606a86ad3ba7ba067d63d8be7e82fad546
```

大规模 `verify` 重新生成并扫描 8+8 个 Shuffle 分区、8 个结果分区，检查提交摘要、
键分区归属、行数守恒、结果指针、账本和规范顺序，最终 `mismatch_count=0`。

## 三算法规模基准

`artifacts/benchmark_report.json` 对每档规模强制运行三种算法。逻辑 tick 不包含墙钟，
结果如下：

| Scale | Algorithm | Rows | Verify | Peak rows | Spill bytes | Logical ticks |
|---|---|---:|---|---:|---:|---:|
| small | broadcast_hash | 387 | true | 300 | 0 | 2.900 |
| small | shuffled_hash | 387 | true | 225 | 0 | 0.800 |
| small | sort_merge | 387 | true | 225 | 108,638 | 2.857 |
| medium | broadcast_hash | 5,677 | true | 598 | 0 | 2.400 |
| medium | shuffled_hash | 5,677 | true | 598 | 0 | 2.400 |
| medium | sort_merge | 5,677 | true | 598 | 314,230 | 13.842 |
| large | broadcast_hash | 53,315 | true | 1,944 | 0 | 7.000 |
| large | shuffled_hash | 53,315 | true | 1,944 | 0 | 7.000 |
| large | sort_merge | 53,315 | true | 1,944 | 941,353 | 83.554 |

大规模强制广播的构建侧为 `285,843` 字节，超过广播上限，因此按明确规则降级为
分区局部哈希；指标中保留 `fallback_events` 和实际算法，没有静默溢出。

## 强制终止与恢复

### Shuffle 中途终止

使用：

```bash
JOIN_ENGINE_CRASH_POINT=shuffle_left_after:2
```

进程退出码 `137` 后状态：

```text
left : committed, committed, partial, partial, partial, partial, partial, partial
right: missing, missing, ...
half-written left temp files: 6
```

`resume` 完成 5,677 行结果，`verify=true`，`mismatch_count=0`，临时文件被清理。

### 联接中途终止

使用：

```bash
JOIN_ENGINE_CRASH_POINT=join_after:2
```

进程退出码 `137` 后：

```text
result partitions: committed, committed, missing, missing, missing, missing, missing, missing
```

恢复指标：

```text
partitions_skipped = 2
partitions_processed = 6
result_rows = 5677
verify = true
mismatch_count = 0
```

之前的 2 个结果分区未重算；死亡进程的 `running` 轮次被标为 `interrupted`，恢复轮次为
`completed`。

## 并发与幂等

两个独立进程同时对同一版本、同一分区执行：

```bash
python -m join_engine run ... --partitions 0
python -m join_engine run ... --partitions 0
```

结果：

```text
worker statuses: waiting, waiting
partition 0 row in partition_commits: 1
partition 0 status: committed
```

随后补跑其余分区，最终状态 `completed`，结果 387 行，`verify=true`。

## 机器可读产物

- `artifacts/benchmark_report.json`：三档 x 三算法完整指标、裁决、观测和真值来源。
- `artifacts/validation_summary.json`：环境、裁决、运行、恢复、并发与测试摘要。

