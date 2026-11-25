# 实际验证记录

验证日期：2026-11-26（Asia/Shanghai）

环境：

```text
Python 3.10.12
SQLite 3.37.2
Linux 6.18.33.2-microsoft-standard-WSL2 x86_64
仅 Python 标准库
无 Docker、无网络调用
```

## 1. 确定性样本

命令：

```bash
python3 generate_samples.py --output samples/default --seed 20261126
```

生成内容：

```json
{
  "layers": 8,
  "replicas": 3,
  "requests": 48,
  "injections": 0
}
```

规范化夹具 SHA-256：

```text
03699b9066e51e6f435e7eabbce3f8a5ac4fa179fa5fdb825895d360aaf6a028
```

## 2. 一次性完整执行

```bash
python3 ledger.py load samples/default --db /tmp/metrics.db
python3 ledger.py run --db /tmp/metrics.db
python3 ledger.py status --db /tmp/metrics.db
python3 ledger.py verify --db /tmp/metrics.db
```

实测：

```json
{
  "windows": 12,
  "batches": 15,
  "batch_size_distribution": {
    "1": 3,
    "3": 5,
    "4": 6,
    "5": 1
  },
  "ledger_entries": 199,
  "skipped_requests": 3,
  "skipped_constraints": {
    "max_batch_size": 3
  },
  "precision_sensitive_windows": 5,
  "request_states": {
    "COMPLETED": 47,
    "EXPIRED": 1
  },
  "verify_ok": true,
  "difference_count": 0,
  "internal_checks_passed": true
}
```

`max_batch_size` 之外的 `compute_budget`、`memory_budget`、`deadline` 首个触发约束
由 `tests/test_constraints.py` 的定向场景分别验证通过。

## 3. 全量自动测试

命令：

```bash
./run_tests.sh
```

最终实测结果：

```text
Ran 25 tests
OK
```

覆盖：

- 合法/非法夹具与稳定业务错误码。
- 非法输入不创建 DB 或 lock；非法 `--reset` 不删除已有库。
- 定点量化、加法/乘法误差传播和完整误差谱。
- 固定种子样本两次生成逐字节一致。
- 逻辑窗口边界、请求唯一归属、双预算、优先级跳过与超期。
- `compute_budget`、`memory_budget`、`deadline` 定向裁决场景。
- `window_open`、`batch_adjudication`、`batch_commit`、`window_commit`、
  `batch_execute` 五个真实 SIGKILL 检查点。
- 半提交窗口识别、续算、零字段差异 verify。
- 4 进程并发、单次提交、重复运行幂等。
- `verify` 对人为字段损坏返回退出码 4 和差异路径。

## 4. SIGKILL 与续算

一键演示：

```bash
./demo.sh
```

实测摘要：

```json
{
  "run_exit": 137,
  "partial_complete": false,
  "half_committed_windows": 1,
  "fired_injections": 1,
  "resume_complete": true,
  "request_states": {
    "COMPLETED": 47,
    "EXPIRED": 1
  },
  "verify_bitwise_match": true,
  "verify_difference_count": 0,
  "internal_checks_passed": true
}
```

Shell 观测退出码 `137` 表示进程被 `SIGKILL`（Python `subprocess` API 中为
`-SIGKILL`）。`batch_commit` 注入后状态为：1 个 `OPEN` 窗口、至少 1 个已提交批、
其余 11 个窗口未推进；`resume` 只补交窗口缺口与未执行批次。
此时 `logical_clock_ms=0`（尚未完整推进任一窗口），`target_clock_ms=1300`，
证明目标时钟与已提交逻辑进度被分别持久化。

五个注入点均由自动测试真实杀进程并续算，全部与一次性基线逐字段一致。

## 5. 并发与幂等

自动测试同时启动 4 个 `run` 进程操作同一副本/同一状态库：

- 4 个进程退出码均为 `0`。
- 仅 1 个进程实际提交，且累计提交窗口数 `12`、批数 `15`。
- 其余进程观察到已完成状态，以 `newly_committed_* = 0` 幂等返回。
- 最终 `verify` 与单进程基线零差异。
- 再次单独运行 `run`，窗口/批/执行计数均为 `0`，`status` 字节级稳定。

## 6. 状态与错误输出

- `status` 使用 `sort_keys=True`，请求/窗口/批/归属/生命周期均按稳定键排序。
- 相同状态下连续两次 `status` 输出完全相同。
- 业务错误 JSON 写 stderr；`E_STATE_NOT_FOUND` 退出 `3`，
  `E_VERIFY_INCOMPLETE` 退出 `4`。
- 非法夹具错误在创建 SQLite 文件前返回，未留下 DB 或 `.lock`。

## 7. 结论

固定种子样本上的批处理窗口裁决、定点误差账本、超期状态、五类强制终止续算、
多进程并发与一次性执行重放比较均实际通过；续算与一次性执行结果逐字段一致。
