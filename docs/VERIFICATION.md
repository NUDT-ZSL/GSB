# 实际验证记录

验证日期：2026-11-25（Asia/Shanghai）。

## 环境

| 解释器 | NumPy | 测试结果 |
| --- | --- | --- |
| Python 3.10.12（容器默认） | 1.26.4 | `18 passed` |
| CPython 3.11.18（`uv python install 3.11`） | NumPy 2.4.x（`numpy>=1.26` 解析） | `18 passed` |
| CPython 3.12.13（`uv run --python 3.12`） | NumPy 2.5.5（`numpy>=1.26` 解析） | `18 passed` |

完整测试命令：

```bash
python -m pytest -q tests/test_balance_lab.py
python scripts/verify_demo.py
```

最终结果：`18 passed`；`scripts/verify_demo.py` 退出码为 `0`，并写出 `artifacts/acceptance_summary.json`。

## 端到端演示

以下命令已在当前工作区实际执行：

```bash
python -m balance_lab.cli generate-samples --output samples
python -m balance_lab.cli run \
  --input samples --state-dir state --reports-dir reports --verify
python -m balance_lab.cli evolve \
  --input samples --work-dir build/edits \
  --state-dir state-evolve --reports-dir reports-evolve
```

基础运行结果：

```text
status=completed
balanced=true
consistent=true
scheduler_rounds=3
admission_count=13
```

三场战斗的定点平衡结论：

| 战斗 | 平衡 | 结局 | 胜利回合 | 队伍血量比例 | 收益分 | 最大定点/浮点残差 | 超差数 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `b_training` | true | victory | 4 | 0.931323 | 111.558906 | 4.965e-07 | 0 |
| `b_core` | true | victory | 5 | 0.917743 | 228.509195 | 4.965e-07 | 0 |
| `b_peak` | true | victory | 12 | 0.794340 | 794.154662 | 1.114e-06 | 0 |

默认残差容差为绝对误差 `5e-6` 加相对误差 `1e-9`，因此样本无超差；自动化测试把容差收紧到 `1e-12`，验证了首个超差回合、单位和技能会被标出。

## 编辑失效与增量一致性

`reports-evolve/invalidation_report.json` 记录了四步顺序编辑。每一步之后都重新执行从头全量推演，并与账本中的增量结果递归比对：

| 步骤 | 变更键 | 直接引用块 | 失效闭包块 | 影响战斗 | 全量一致性 |
| --- | --- | ---: | ---: | --- | --- |
| 1 | `unit:e_grunt_a:defense` | 7 | 7 | `b_core`, `b_training` | true |
| 2 | `skill:sk_priest_heal:power` | 8 | 10 | 全部三场 | true |
| 3 | `drop_table:dt_core:weight:gold` | 4 | 4 | `b_core` | true |
| 4 | `criterion:cr_peak:min_team_hp_ratio` | 6 | 6 | `b_peak` | true |

步骤 2 展示了状态边界传播：`b_training` 只失效第 1、2 块，第 0 块保留并作为第 1 块的起始状态；`b_core` 和 `b_peak` 中未直接引用治疗技能的后续块也因生命状态可能变化而进入闭包。

每步生成：

- `reports-evolve/invalidation_report.json`
- `reports-evolve/balance_report.step-01.json`
- `reports-evolve/balance_report.step-02.json`
- `reports-evolve/balance_report.step-03.json`
- `reports-evolve/balance_report.step-04.json`
- `reports-evolve/balance_report.json`

主报告通过 `expected_existing_version_id` 做版本 CAS 替换；测试 `test_report_version_compare_and_swap` 验证旧版本不匹配时返回 `E_REPORT_VERSION_CONFLICT`。

## 非法输入

测试 `test_invalid_input_returns_business_error_without_ledger` 参数化覆盖：

- 重复单位 ID：`E_DUPLICATE_ID`
- 负逻辑时刻：`E_BAD_LOGIC_TIME`
- 目标不存在：`E_TARGET_NOT_FOUND`
- 定点标度无法精确量化：`E_FIXED_NOT_QUANTIZABLE`
- 平衡阈值越界：`E_THRESHOLD_DOMAIN`

CLI 输出形如：

```json
{
  "ok": false,
  "error": {
    "code": "E_DUPLICATE_ID",
    "message": "duplicate unit id: u_hero",
    "details": {"id": "u_hero"}
  }
}
```

该路径在创建 `Ledger` 前完成，因此 `--state-dir/balance.db` 不会产生。

## 调度与预算

默认配置为 `parallelism=3`、单轮逻辑预算 `24ms`、内存缓冲预算 `131072B`。最终报告中的调度轮为 3 轮，准入 13 个分块；每轮都有确定的准入顺序、预留字节数和逻辑时钟读数。

测试覆盖：

- `parallelism=1` 与 `parallelism=4` 的分块摘要、最终状态摘要完全相同。
- 相同 `parallelism=4` 连续运行两次，准入序列完全相同。
- `memory_budget_bytes=128` 时返回 `E_MEMORY_CHUNK_EXCEEDS_BUDGET`。

## 中断与强杀恢复

自动化测试包含：

- `test_graceful_sigterm_then_restart_consistent`：子进程运行中发送 `SIGTERM`，断言退出码为 `130`、`state/interrupt.json` 存在；重启后 `verify=true` 且增量/全量一致，中断标记被清除。
- `test_sigkill_restart_uses_committed_digests`：POSIX 下发送 `SIGKILL`，重启前账本显示战斗未全部完成；重启后摘要校验、补算和一致性均通过。
- `test_corrupt_chunk_digest_is_pruned_and_recomputed`：人为破坏 `state_blob` 后，账本摘要校验返回 `state_digest_mismatch`，清理并重算后恢复一致。

此前手工演练也观察到：

```text
SIGTERM exit code: 130
SIGKILL exit code: 137
restart consistency: true
```

Windows 上没有 POSIX `SIGKILL` 测试；可用任务管理器或 `Stop-Process -Force` 强杀进程，然后重新执行同一条 `run` 命令。优雅停止可使用 Ctrl+C 或 Ctrl+Break。

## 平衡失败诊断

`test_balance_checks_detect_defeat_timeout_and_reward_imbalance` 验证：

- 将玩家生命压到 1、斥候攻击提高后，战斗结局为 `defeat`，`not_certain_defeat` 判据失败。
- 将 `max_rounds` 改为 1 后，`round_budget` 判据失败，用于识别必然/过晚结束。
- 将 `expected_reward_score` 提高到 `10000` 后，`reward_lower_bound` 判据失败，用于识别收益失衡。

## Windows 交付说明

- 当前命令与信号恢复自动化在 Linux 容器实际执行；没有在真实 Windows 会话中运行，因此不声称已做 Windows 实机验证。
- 所有路径均由 `pathlib.Path` 处理。
- 报告临时文件创建在目标报告同目录，`os.replace` 在 Windows/POSIX 上均为整体替换。
- SQLite 使用 Python 标准库 `sqlite3`，并设置 `synchronous=FULL`。
- 不依赖 Docker、外部服务、网络账号或容器环境。
- Windows 可运行 `python scripts/verify_demo.py` 做跨平台端到端验收；原生 Windows 下 POSIX `SIGKILL` 子进程测试会自动跳过，可用 `Stop-Process -Force` 做手工强杀恢复演练。
