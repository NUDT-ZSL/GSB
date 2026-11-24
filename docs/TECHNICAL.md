# 技术说明与验证记录

## 1. 组件边界

`sso_ticket_guard` 是可嵌入的纯标准库组件：

- `normalize.py`：逐记录解析和规范化，非法记录转为带来源类别和 1-based
  记录序号的 `Issue`，合法记录继续进入数据集。
- `fingerprint.py`：平台定点值到六位小数规范空间的缩放、误差界和成对比较。
- `engine.py`：设备绑定、票据代数/有效期、双配额裁决及可重放事件。
- `revocation.py`：设备到票据、票据到后继票据的传递闭包与环检测。
- `checkpoint.py`：SQLite 提交标记、本地快照、重放校验和文件锁。
- `cli.py`：四类子命令、JSON 原子报告和 `0/1/2` 退出码。

业务时刻只来自请求的 `observed_at_ms`。生产代码没有导入 `time`、`datetime`
或网络库。

## 2. 规范指纹与误差

规范空间固定为 `CANONICAL_DECIMALS = 6`。平台声明小数位数 `p` 时：

```text
alignment_step = 10 ** (6 - p)
canonical_component = raw_component * alignment_step
component_error = alignment_step // 2
```

分量比较阈值是精确相等（阈值 `0`）。对两个分量计算：

```text
margin = abs(a - b) - (error_a + error_b)
```

- 所有分量规范值完全相等：匹配。
- 某个 `margin > 0`：确定不符，返回 `E_DEVICE_MISMATCH`。
- 不存在确定不符，但规范向量不全相等：返回 `E_FP_AMBIGUOUS`，并保留双方
  平台、原始小数位数、原始分量、规范分量和误差界。

样本 `r-fp-ambiguous` 将 iOS 三分量 `[123,456,789]`（3 位小数）与 Android
`[1234,4560,7890]`（4 位小数）对齐到六位空间。第一分量差为 `400`，合并误差
为 `550`，因此不能判同也不能判异。

## 3. 裁决顺序

1. 票据存在性。
2. 请求设备标识与票据绑定设备标识。
3. 请求规范指纹与绑定设备的规范指纹。
4. 代数是否达到续期上限。
5. 观测时刻是否落在 `[issued_at_ms, expires_at_ms)`。
6. 次数窗口配额。
7. 最短间隔配额。

前置的确定性身份/票据失败优先返回；只有前置检查通过后才由配额改变结论。
配额阶段若任一项存在量化不确定，返回 `E_QUOTA_AMBIGUOUS`；确定失败时固定先
检查次数窗口，因此双失败报告的 `first_rejection` 为 `count_window`。

## 4. 双配额与边界公式

次数配额声明使用整数秒。秒到毫秒的换算步长为 `1000 ms`，单端点半量化误差
为 `500 ms`。窗口起点由锚点秒导出，终点还包含窗口长度秒，因此窗口端点误差
上界取 `1000 ms`。再加请求平台时钟定点对齐误差 `clock_quantum_ms // 2`：

```text
window_error_ms = 1000 + clock_quantum_ms // 2
count_margin_slots = limit - prior_accepted_in_window
boundary_distance_ms = min(observed - window_start, window_end - observed)
```

当 `boundary_distance_ms <= window_error_ms` 时，请求属于哪个逻辑窗口不确定。

最短间隔直接以毫秒比较：

```text
interval_anchor = last_accepted_renewal or ticket.issued_at_ms
interval_margin_ms = observed - interval_anchor - min_interval_ms
interval_error_ms = request_clock_error + anchor_clock_error
```

间隔阈值恰好命中时允许；余量落入 `[-interval_error_ms, interval_error_ms]`
时是不确定，只有余量严格小于 `-interval_error_ms` 才是确定拒绝。

报告同时给出 `count_slots`、`interval_ms`、`count_window_boundary_ms`、窗口
起止、锚点来源、两项误差界和 `first_rejection`。

## 5. 撤销闭包

图节点为票据。边来自：

- 票据声明的 `parent_ticket_id` 到后继票据。
- 若导入检查点事件，已接受续期请求的 `ticket_id` 到 `successor_ticket_id`。

设备撤销先映射到该设备的全部票据，再做后继 BFS；票据撤销从目标票据 BFS。
环检测只在从撤销根可达的子图中执行，返回 `E_TICKET_CYCLE` 和稳定排序的环中
票据。闭包实体按 `(type, id)` 排序，因此与声明导入顺序无关。

## 6. 检查点协议

检查点目录包含：

- `checkpoint.sqlite3`：提交标记和完整 payload。
- `checkpoints\ckpt-<sequence>-<sha256>.json`：同 payload 的本地原子快照。
- `checkpoint.lock`：操作系统级非阻塞独占锁；Windows 使用 `msvcrt`，POSIX
  使用 `fcntl`。

保存顺序为“原子快照 -> SQLite 事务提交”。恢复时只选择数据库中存在、快照
存在、双份 payload 校验和一致、结构/配额版本一致，且从事件重放得到的计数
状态与摘要完全一致的最后一个检查点。快照先写但事务未提交的残留会被列为
`orphan_snapshots`；最新行缺快照或 payload 不一致会回退到更低序列的完整
检查点。任何结构或配额口径版本变化都会清空旧行和快照，整体重建。

`renew --state-dir` 将累计事件重放到 `RenewalState`，按 `batch_id` 水位跳过
已完成批次。锁冲突返回 `E_LOCK`，不会修改数据库或快照。

## 7. 固定样本的可复现结论

种子：`20261125`。规范化后的业务结果：

| 场景 | 请求 | 结论 |
| --- | --- | --- |
| 绑定不符 | `r-bind-mismatch` | `E_DEVICE_MISMATCH` |
| 跨平台指纹不确定 | `r-fp-ambiguous` | `E_FP_AMBIGUOUS` |
| 次数与间隔双冲突 | `r-count-both-fail` | `E_QUOTA_COUNT`，首拒绝 `count_window` |
| 仅间隔拒绝 | `r-interval-reject` | `E_QUOTA_INTERVAL`，余量 `-1000 ms` |
| 间隔边界不确定 | `r-edge-ambiguous` | `E_QUOTA_AMBIGUOUS`，余量 `30 ms`，误差界 `50 ms` |
| 日窗口端点不确定 | `r-daybound-ambiguous` | `E_QUOTA_AMBIGUOUS`，边界 `300 ms`，误差界 `1000 ms` |
| 票据过期 | `r-expired` | `E_TICKET_EXPIRED` |
| 代数超限 | `r-generation` | `E_GENERATION_EXCEEDED` |
| 撤销设备 | `rev-dev-chain` | 6 张链票据传递失效 |
| 撤销票据 | `rev-ticket-a1` | `chain-a-1`、`chain-a-2` |
| 环状依赖 | `rev-cycle` | `E_TICKET_CYCLE`，`cycle-0`、`cycle-1` |

有效续期请求汇总：5 次允许、3 次不确定、5 次拒绝。13 条非法声明被逐条拒绝，
但没有阻止其余合法声明处理。

## 8. 实际验证

本次实现已在当前离线工作区执行：

```text
python generate_samples.py --output samples
generated 6 declaration files; seed=20261125

python -m pytest
24 passed

python -m sso_ticket_guard.cli renew --state-dir .sso-state --fresh
5 accept / 3 ambiguous / 5 reject; checkpoint watermark=5

python -m sso_ticket_guard.cli checkpoint verify --state-dir .sso-state
status=resumed; replay_verified=true

# 两阶段中断演练
renew --fresh --max-batches 2  -> processed_batches=[1, 2], stopped_early=true
renew (same state directory)   -> processed_batches=[3, 4, 5]
checkpoint verify              -> watermark=5, unique_events=13, replay_verified=true
```

测试覆盖绑定不符、跨平台指纹不确定、次数/间隔双冲突、时间边界不确定、环状
依赖、JSON 原子报告、退出码、检查点版本重建、孤儿快照、损坏行回退、锁冲突及
两阶段中断恢复。当前验证环境为 Python 3.10.12 的 WSL 本地环境；代码不使用
平台特有 API，Windows 锁分支使用 `msvcrt`，目标运行环境仍为 Python 3.11+
的 Windows 10/11。

## 9. 明确限制

- 样本生成器和真值文件仅用于本地确定性验证，不是身份提供商。
- 规范指纹用于原型中的设备绑定比较，不等同于生产环境的抗碰撞硬件证明。
- 检查点保存所有已处理事件以支持完整重放；生产规模应改为分段事件日志或
  增量 Merkle 摘要。
- 文件锁串行化同一工作目录的写者，不提供跨机器分布式事务。

