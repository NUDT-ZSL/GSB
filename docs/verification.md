# 实际验证记录

验证日期：2026-11-20（Asia/Shanghai）

## 环境

```text
Python 3.10.12
NumPy 2.2.6
SQLite 3.x (Python 标准库 sqlite3)
```

NumPy 安装在项目 `.venv` 中：

```bash
uv venv --python 3.10 --seed .venv
.venv/bin/python -m pip install numpy
.venv/bin/python -c 'import numpy; print(numpy.__version__)'
```

实测输出：

```text
2.2.6
```

## 自动化测试

命令：

```bash
.venv/bin/python -m unittest discover -v
```

结果：

```text
Ran 18 tests in 4.125s
OK
```

覆盖内容：

- 四文件与单文件场景摘要一致。
- RTT、脚本顺序、未来字节超时引用等非法输入错误码。
- 未建连写数据、时钟回拨、提前超时均不改变状态。
- 被动建连、CLOSE-WAIT、LAST-ACK 全链迁移。
- 慢启动、拥塞避免、快速重传、部分确认、接收窗口缩放。
- 定点 cwnd 两次推演逐字节一致。
- 超时重试耗尽进入 `FAILED` 并持久化。
- 两次独立运行的 `run/report/verify` 输出逐字节一致。
- 强制终止后只重放剩余事件，恢复结果与干净运行一致。
- 摘要变化拒绝推演且不创建新版本。
- 四进程分段、重复提交幂等和最终轨迹验证。

## 固定样本

样本事件数：24。

```text
source_digest:
994c4f0e8b3041004873e29a8ca290b598ac2ae7e6e412f496e22236c2c0f94e

trace_digest:
3178e8908a741a56232b1c3e98d2b5afbfd92fb0c74f3ed52de726156c158b92
```

状态迁移：

```text
LISTEN -> SYN-SENT -> ESTABLISHED
ESTABLISHED -> FIN-WAIT-1 -> FIN-WAIT-2 -> TIME-WAIT -> CLOSED
```

重传：

```text
fast_retransmit          seq 6048..7048
partial_ack_retransmit   seq 7000..7048
timeout attempt 1        seq 11000..11048
timeout attempt 2        seq 11000..11048
timeout attempt 3        seq 11000..11048
```

最终 cwnd 固定值 `3374545 / 1024`，精确十进制字符串为
`3295.4541015625`。该值说明窗口计算确实使用受控定点数，而非浮点数。

## 两次运行逐字节一致

命令：

```bash
./scripts/demo.sh
```

实测稳定摘要：

```json
{"crash_committed_events":7,"crash_resume_replayed_events":17,"demo":"deterministic-tcp-demo","report_sha256":"ae81cf64ab92f4bdc8b60b4cc75aef4f7ac90b4559a635c0a230f0c98ae6c1b2","reports_byte_identical":true,"resumed_report_matches_clean_report":true,"run_outputs_byte_identical":true,"source_digest":"994c4f0e8b3041004873e29a8ca290b598ac2ae7e6e412f496e22236c2c0f94e","trace_digest":"3178e8908a741a56232b1c3e98d2b5afbfd92fb0c74f3ed52de726156c158b92","verified_events":24}
```

脚本执行了：

1. 在 `run-a.sqlite` 与 `run-b.sqlite` 两个独立数据库上各运行一次。
2. 使用 `cmp` 比较两次 `run` stdout，字节相同。
3. 使用 `cmp` 比较两个 `report` stdout，字节相同。
4. 对两个数据库分别执行 `verify`，均为 24 个事件一致。
5. 在第三个数据库运行到第 7 个事件时 `os._exit(70)`。
6. `status` 显示最新版本 `STAGING`、连续提交 7 个事件、无活动完整版本。
7. `resume` 只执行剩余 17 个事件。
8. 恢复后的报告与干净运行报告 `cmp` 相同。

## 并发分段

命令：

```bash
./scripts/concurrent.sh
```

实测输出：

```json
{"active_version_status":"COMPLETE","committed_events":24,"duplicate_submit_idempotent":true,"parallel_segments_completed":true,"trace_digest":"3178e8908a741a56232b1c3e98d2b5afbfd92fb0c74f3ed52de726156c158b92","verified_events":24}
```

四个 worker 分别处理：

```text
[0, 6) [6, 12) [12, 18) [18, 24)
```

所有 worker 使用同一个 `--version-tag parallel`。最后一个连续区间落盘后，版本在
同一事务中切换为 `COMPLETE` 并成为 `current_version`。随后再次提交 `[0, 6)`，
所有事件结果与已有行相同，重复提交为幂等 no-op。

## 边界验证

- 逻辑时钟回拨：`E_CLOCK_REWIND`，状态快照不变。
- 未到 RTO deadline 的超时：`E_TIMEOUT_NOT_DUE`，状态快照不变。
- 未建连发送数据：`E_BAD_TRANSITION`，状态快照不变。
- 超时引用尚未写入的字节：加载阶段 `E_BAD_SCRIPT`。
- 源脚本变更后复用 `run_id`：`E_DIGEST_MISMATCH`，版本数不增加。
- 重试预算耗尽：`E_RETRY_EXHAUSTED`，活动版本状态为 `FAILED`，失败状态可查询。

