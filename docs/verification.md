# 实际验证记录

验证日期：2026-11-21

## 环境

| 项目 | 实际值 |
| --- | --- |
| Python | 3.10.12 |
| SQLite | 3.37.2 |
| NumPy | 1.26.4 |
| pytest | 8.3.3 |
| 平台 | Linux WSL2 x86_64 |

代码目标兼容 Windows + Python 3.11；当前工作区没有 Windows/3.11 解释器，因此本次
实际执行环境为 Python 3.10.12。代码只使用 Python 3.10 及以上语法，并在
`pyproject.toml` 中声明 `>=3.10`。POSIX 文件锁已实测；Windows `msvcrt` 分支使用
非阻塞重试加 60 秒超时。

## 自动化测试

命令：

```bash
python -m pytest --override-ini addopts= -q
```

结果：

```text
......................................                                   [100%]
40 passed in 10.85s
```

覆盖范围：

- 端点/申请唯一性、回环地址、正整数、非负持有时长、最小空闲边界、时钟步进；
- 非法样本零持久化、显式摘要不匹配、配置版本回退、同版本配置冲突；
- 租约新建、空闲复用、每租户配额排队、总容量排队、跨租户禁止复用；
- 精确/分层选择序列逐位一致、成本迟滞切换；
- `IDLE_TIMEOUT`、`QUOTA_SHRINK`、`ACTIVE_CLOSE` 三类关闭；
- 配置收缩后活动租约超过新配额时，在后续释放事务中延迟回收超额空闲连接；
- 最小空闲保护、活动租约不被回收；
- 回环服务真实回显、拒绝连接、畸形探针、建链预算超限；
- 六个强制终止点及恢复一致性、重复扣减防护；
- 12 进程并发申请不超建、8 进程重复申请/释放幂等；
- CLI 稳定 JSON、两次自动重放一致、exact/tiered 等价性。

## 真实回环演示

命令：

```bash
python scripts/demo.py
```

演示脚本启动 `127.0.0.1:19091` 与 `127.0.0.1:19092` 两个回环回显端点，执行 CLI
命令，并连续运行两次 `verify --transport loopback`。

关键结果：

- 两次 `verify` 输出完全相同：`true`
- 两次自动模式重放状态一致：`true`
- exact/tiered 租约发放序列一致：`true`
- 发放租约：`10`
- 复用空闲连接：`2` 次（申请 9 复用 `conn-00000001`，申请 10 复用
  `conn-00000002`）
- 模式切换：`4` 次
- 超时回收：`6` 个空闲连接，关闭原因均为 `IDLE_TIMEOUT`
- 最终池水位：`total_allocated=2`、`total_active=0`、`total_idle=2`
- 恢复：修复行 `0`、中止遗留意图 `0`、重建回环句柄 `6`、重建错误 `0`

模式切换链：

```text
20ms  exact  -> tiered
25ms  tiered -> exact
25ms  exact  -> tiered
100ms tiered -> exact
```

最终状态摘要：

```text
sample_digest = 0786e8896ea5677f538a3179065108be6fe5c292823de4ffe49ba10b966b54bb
state_sha256  = b37d1d7be7d885c82ab1a60ece0a62d6a4fc74a864ede3e2e6ec14100dc2fea8
idle_digest   = 5da1695578f1ee24b0d802e2985f283051c879941ba3704c72259fc290a71fd8
```

完整状态报告：

- `artifacts/status-report.json`
- `artifacts/verification-summary.json`

## 故障注入验证

以下崩溃点均由真实子进程退出码 91 触发，随后用 `status`/`resume` 检查：

| 崩溃点 | 验证结果 |
| --- | --- |
| `after_connect_before_commit` | 无租约、无连接计数；遗留 `OPENING` 意图被 resume 中止 |
| `after_rows_before_commit` | 请求、租约、连接、配额整笔回滚；计数为 0 |
| `after_commit_before_ack` | 租约可见；相同申请重试返回同一 `lease_id`，计数仍为 1 |
| `release_after_state_before_commit` | 租约仍为 `ACTIVE`；重试释放成功，第二次释放幂等 |
| `resume_after_repair_before_commit` | 修复事务回滚；再次 resume 后连接数仍为 1，不重复计数 |

并发测试另外验证：4 个容量槽位下 12 个并行申请只发放 4 个租约，其余 8 个进入
队列；8 个进程提交同一申请只产生 1 个租约。

## 可重复性说明

状态报告不包含墙钟、PID、随机 UUID 或 socket 句柄。连接、租约 ID 来自持久化单调
序号；事件、队列、租约、空闲桶均按固定键排序；成本公式只使用整数逻辑时钟和
确定性浮点运算。因此相同样本、相同模式、相同逻辑时钟序列会得到逐位一致的 JSON。
