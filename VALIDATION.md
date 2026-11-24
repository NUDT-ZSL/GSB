# 实际验证记录

验证时间：2026-11-25（Asia/Shanghai）。

## 运行环境

当前执行外壳为 Ubuntu/WSL 风格工作区；工程代码使用 `pathlib`、标准库 `sqlite3` 和条件导入的 `fcntl`/`msvcrt`，README 另给 Windows PowerShell 操作路径。文件锁的 `msvcrt` 分支未在本次 Linux 运行中实机执行。

```text
Python 3.11.18
SQLite 3.53.1
pytest 9.2.0
numpy 2.4.6
```

虚拟环境位于 `.venv/`，已用 Python 3.11 创建并安装 `pytest`、`numpy`、`pip`。

## 1. 固定种子样本与同步标记

命令：

```bash
.venv/bin/python -m osl_forensics.cli generate \
  --out samples --plan anomaly_injections.json
.venv/bin/python -m osl_forensics.cli verify-injections \
  --root samples/injections \
  --report .osl-work/verify.json
```

实际结果：

```json
{"ok": true, "plan": "anomaly_injections.json", "samples": 22}
{"failed": 0, "ok": true, "passed": 22, "total": 22}
```

覆盖的期望业务码包括：

- 结构：`E_HEADER`、`E_BOUNDS`（含 uint64 溢出）
- 区间/名称：`E_DUP_NAME`、`E_SELFREF`、`E_OVERLAP`
- 完整性：`E_CHECKSUM`、`E_CHECK_ALGO`、未知算法
- 嵌套：`E_DEPTH`、`E_CYCLE`、`E_QUOTA`
- 预算：`E_TIMEOUT`
- 布局：默认 `osl40` 与字面 16 字节保留区的 `literal42`

每个用例目录均包含 `sample.osl` 与 `sync_marker.json`；验证器只使用 marker 中的 `expect_code`、`expect_offset` 和复现配置。所有生成样本头保留区均含二进制同步字 `OSLSYNC1`。

## 2. pytest 全量测试

命令：

```bash
.venv/bin/python -m pytest -q
```

实际结果（最终新增引擎配额接线与确定性重建测试后）：

```text
24 passed
```

最终全量测试在虚拟环境 Python 3.11 与系统 Python 3.10 均为 `24 passed`。开发过程中曾连续执行 5 轮完整测试，未出现并发、文件锁或子进程恢复抖动。

测试到需求的映射：

- `tests/test_injection_markers.py`：全部同步标记的业务码与偏移。
- 固定种子样本在两个独立目录中逐字节一致。
- `tests/test_structural_validation.py`：头部、uint64 溢出、UTF-8、重复名真值、自引用、重叠。
- `tests/test_integrity_and_nesting.py`：SHA-256/HMAC、摘要位宽、未知算法、嵌套规范键、深度、环、配额。
- `tests/test_ledger_and_revisions.py`：前置失败零写入、幂等重放、策略指纹变化整体重建、传递失效闭包和批次存储顺序无关。
- `tests/test_concurrency_budget.py`：串行/8 并发/随机到达顺序的段状态与事件哈希一致、队列拒绝一致、墙钟超时零写入。
- `tests/test_checkpoint_lock_recovery.py`：`E_LOCK`、快照版本识别、最后完整检查点恢复，以及真实子进程 `os._exit(137)` 模拟强杀后的恢复。

## 3. SQLite 账本与检查点

命令：

```bash
.venv/bin/python -m osl_forensics.cli process \
  --root samples/injections \
  --db .osl-work/validation/ledger.sqlite \
  --workdir .osl-work/validation \
  --report .osl-work/validation/batch-report.json \
  --concurrency 4 --queue 30

.venv/bin/python -m osl_forensics.cli recover \
  --workdir .osl-work/validation \
  --db .osl-work/validation/ledger.sqlite
```

实际结果：

```json
{
  "accepted": 22,
  "counts": {
    "E_BOUNDS": 2,
    "E_CHECKSUM": 1,
    "E_CHECK_ALGO": 2,
    "E_CYCLE": 1,
    "E_DEPTH": 1,
    "E_DUP_NAME": 1,
    "E_HEADER": 6,
    "E_OVERLAP": 2,
    "E_SELFREF": 1,
    "OK": 5
  },
  "rejected": 0,
  "replay_ok": true
}
```

```json
{
  "ok": true,
  "replay": {
    "events": 26,
    "hash_failures": [],
    "mismatches": [],
    "ok": true,
    "states": 26
  },
  "restored_snapshot": false,
  "sequence": 22
}
```

这表示正常关闭路径下当前 SQLite 事件尖与第 22 个完整检查点一致，无需恢复快照；事件哈希链和状态投影重放无不一致。

## 4. 队列预算

命令：

```bash
.venv/bin/python -m osl_forensics.cli process \
  --root samples/injections \
  --db .osl-work/queue/ledger.sqlite \
  --workdir .osl-work/queue \
  --report .osl-work/queue/report.json \
  --concurrency 8 --queue 3
```

实际结果：

```json
{"accepted": 3, "counts": {"E_HEADER": 3, "E_QUEUE_FULL": 19}, "rejected": 19, "replay_ok": true}
```

拒绝集合按 `file_id` 稳定排序；测试验证反向到达和 4 并发产生完全相同的拒绝记录。

## 5. 修订失效闭包

固定样例：

```bash
.venv/bin/python -m osl_forensics.cli revise-fixture \
  --fixture samples/revision_fixture.json \
  --out .osl-work/validation/revision-closure.json
```

实际重算集合：

```json
["/agg", "/raw", "/report"]
```

变化来源为 `/raw`；`/agg` 的最小依赖距离为 1，`/report` 为 2。打乱段、边和批次登记顺序后结果相同。

## 未做事项

- 未使用 Docker 或其他容器。
- 未访问网络账号、外部服务或未授权目标；样本由本地固定种子重建。
- 未在真正的 Windows 进程中执行命令；Windows 路径和 `msvcrt` 锁分支已按标准 API 编写，建议交付前在 Windows 上再运行一次 `pytest -q`。
