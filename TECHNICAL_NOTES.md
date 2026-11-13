# 凭据复用检测工具（Credential Reuse Detector）

## 1. 概述

`credential_reuse_detector.py` 读取 Windows 风格安全日志的确定性 CSV 导出，检测
**同一账户在滑动时间窗口内从至少 2 台不同主机成功登录**的凭据复用/横向移动迹象，
以 JSON Lines 向 stdout（或 `--output` 文件）输出警报，并用退出码区分运行结果。

仅使用 Python 标准库（`argparse, csv, json, os, sys, collections, dataclasses,
datetime, typing`），Python 3.10+ 即可运行，本机验证版本为 Python 3.13.9。

## 2. 文件清单

| 文件 | 作用 |
|---|---|
| `credential_reuse_detector.py` | 检测工具主程序（CLI 入口） |
| `build_samples.py` | 确定性构造 `demo_data/` 下全部测试 CSV |
| `run_acceptance_tests.py` | 覆盖需求 1–15 的 52 项自动化验收检查 |
| `demo.run.json` | 录屏交付配置（terminal demo，15 个真实命令） |
| `demo_data/` | 本地确定性样本（由脚本生成，可重复重建） |
| `demo_output/` | 演示产生的警报与 `error_events.log`（git 忽略） |

## 3. 用法

```text
python credential_reuse_detector.py CSV_PATH [--window MINUTES]
                          [--logon-types TYPES] [--output PATH]
                          [--error-log PATH]
```

- `--window`：窗口分钟数，整数 1–1440，默认 10；非整数或越界 → 退出码 2。
- `--logon-types`：逗号分隔的整数白名单，默认 `2,3,10`；含非整数 → 退出码 2。
- `--output`：警报文件，默认 `-`（stdout）；目标不可写 → 退出码 1。
- `--error-log`：坏行追加日志，默认 `error_events.log`，父目录自动创建。

## 4. 关键设计决策

### 4.1 滑动窗口与警报合并（需求 3、7、14）

每个账户维护一个按时间有序的 `deque[LoginEvent]`：

1. 新事件到达时，先从队首淘汰 `newest_ts - old_ts > window` 的过期事件；
2. 若窗口内剩余事件不足 2 条，关闭该账户可能处于打开状态的警报簇；
3. 加入新事件后重新统计窗口内不同主机数；
4. 不同主机数 ≥ 2 时打开/续接**同一个** `AlertCluster`，`event_count` 累加、
   `hosts` 取并集、`last_time` 推进——因此窗口内 3 台主机的 3 次登录只产生
   1 条 `event_count=3` 的警报；
5. 窗口滑到只剩 1 台主机时簇关闭并落盘，下一次再满足条件会形成新簇。

窗口边界采用闭区间：间隔恰好 10 分钟的事件仍在窗口内（已测试）。

### 4.2 1000 条内存上限（需求 14）

全局计数 `total_events` 跟踪当前驻留的成功事件总数。正常情况下滑动窗口自然淘汰
过期事件；仅当窗口内事件确实超过 1000 条时，才强制淘汰**全局最早**的一条
（扫描各账户队首取最小值）。因此窗口内可触发警报的事件不会因常规清理丢失。
验收中以 30 秒间隔灌入 1100 条事件：驻留数始终 ≤ 1000，仍输出 1 条
`event_count=1100` 的合并警报。

### 4.3 时间处理（需求 2、9）

- 统一走 `parse_timestamp()`：`Z`/`z` 后缀先转为 `+00:00` 再交
  `datetime.fromisoformat`（兼容 3.10）；
- 无时区时间戳按 UTC 处理；带偏移量一律 `astimezone(timezone.utc)` 归一；
- 输出统一为 UTC ISO 8601（`...+00:00`），如 `2026-11-13T10:00:00+08:00`
  输出为 `2026-11-13T02:00:00+00:00`；
- 解析失败不中断处理，原始行写入 `error_events.log` 后跳过。

### 4.4 坏行隔离与错误日志（需求 2、8、11）

- 空行与 `#` 开头的注释行直接忽略；
- 字段数 < 5 的行记 `short_row`；坏时间戳记 `bad_timestamp`；
  非整数 `logon_type` 记 `bad_logon_type`；
- 日志按物理行读取后再用 `csv` 解析，保证记录的是**原始行内容**；
- 追加模式打开；每行格式为 `UTC处理时间<TAB>错误类型<TAB>原始行`，
  并硬性保证单行长度 ≤ 原行长度 + 200 字符。

### 4.5 表头校验（需求 1）

首个非空非注释行须包含 `time, account, host, logon_type, result` 五列
（允许额外列，按列名定位索引）；缺列打印 `error: invalid CSV header: ...`
并退出码 2。

### 4.6 成功判定与白名单

- `result` 不区分大小写，`success / succeeded / 0` 视为成功，其余（含
  `failure`）忽略；
- `logon_type` 不在白名单的事件直接忽略，不进入窗口、不占内存。

### 4.7 确定性排序（需求 10）

全部事件处理完后统一 `finalize()`，警报按 `(first_time, account)` 升序排序，
`hosts` 数组也按字典序输出，保证同输入字节级结果可重复。

### 4.8 退出码与优先级（需求 13）

| 码 | 含义 |
|---|---|
| 0 | 无警报且无运行时错误 |
| 1 | 运行时错误（输入不可读、输出/日志不可写等） |
| 2 | 参数错误（argparse）或表头错误 |
| 3 | 产生了 ≥1 条警报 |

同时出现时优先级 `1 > 2 > 3 > 0`。实现顺序保证：输出文件先于 CSV 表头打开，
故「表头错误 + 输出不可写」组合返回 1（有自动化测试覆盖）。行级坏数据属于
被隔离的数据质量问题，不属于致命运行时错误。

## 5. 实际验证结果

### 5.1 自动化验收（`python run_acceptance_tests.py`）

最近一次运行结果：**52/52 checks passed**，逐条对应任务需求：

| 需求 | 覆盖检查（摘选） | 结果 |
|---|---|---|
| 1 表头校验 | 缺列 → 码 2 + stderr 错误 | PASS |
| 2 坏时间戳隔离 | 坏行进日志、好行照常警报、程序正常结束 | PASS |
| 3 默认 10 分钟检测 | alice 5 分钟内 H1/H2 → 1 条警报、码 3；`--window 4` → 无警报码 0 | PASS |
| 4 JSON 字段 | 五字段完整、时间可解析且 UTC 偏移为 0 | PASS |
| 5 `--window` 校验 | `0`、`abc`、`1441` 均码 2，`0` 的报错含 1–1440 范围 | PASS |
| 6 登录类型白名单 | `--logon-types 2` 后 type=3 事件不触发；`2,x` → 码 2 | PASS |
| 7 跨主机合并 | 3 主机 → 1 条警报、`event_count=3`、hosts 并集为 3 | PASS |
| 8 空行/注释/短行 | 短行进 `short_row` 日志且含原内容，程序不崩溃 | PASS |
| 9 时区归一 | `+08:00` 事件输出 `02:00:00+00:00`，`Z` 后缀可解析 | PASS |
| 10 排序 | ordering 样本输出 alice 先于 bob | PASS |
| 11 追加错误日志 | 二次运行文件增长；每行含时间戳、错误类型、原行；长度 ≤ 原行+200 | PASS |
| 12 `--output` | 指向现存目录 → 码 1 + 报错；正常文件写入成功、码 3 | PASS |
| 13 退出码 | 0/1/2/3 四种路径 + `1>2` 优先级组合 | PASS |
| 14 内存上限 | 1100 事件驻留 ≤1000 且警报 `event_count=1100`；1100 行文件与双簇（550+550）均正确 | PASS |
| 15 `--help` | 含三个参数名及默认值 10 / 2,3,10 / stdout | PASS |

### 5.2 手工冒烟（真实输出节选）

基本检测：

```text
$ python credential_reuse_detector.py demo_data/basic.csv
{"account": "alice", "hosts": ["HOST-H1", "HOST-H2"],
 "first_time": "2026-11-13T01:00:00+00:00",
 "last_time": "2026-11-13T01:05:00+00:00", "event_count": 2}
exit=3
```

坏行日志（追加模式，含处理时间、类型、原始行）：

```text
2026-11-13T11:40:30.897600+00:00  bad_timestamp  not-a-timestamp,alice,HOST-H2,3,success
2026-11-13T11:40:30.897819+00:00  short_row      2026-11-13T02:06:00,alice,HOST-H9,2
```

时区归一：`2026-11-13T10:00:00+08:00` 与 `2026-11-13T02:04:00Z`
→ 警报 `first_time` 为 `2026-11-13T02:00:00+00:00`。

边界补充测试：单主机重复登录不报警；纯失败事件不报警；间隔恰好 10 分钟
在窗口内（1 条警报）；间隔 10 分 01 秒则不报警。

## 6. 复现步骤

```bash
python build_samples.py            # 确定性生成 demo_data/ 与 demo_output/
python run_acceptance_tests.py     # 52 项验收，期望 52/52 passed
python credential_reuse_detector.py demo_data/basic.csv          # 单条警报，码 3
python credential_reuse_detector.py demo_data/volume_1100.csv    # 大容量流
```

全程仅使用本地合成样本，无网络访问、无容器、无第三方依赖。
