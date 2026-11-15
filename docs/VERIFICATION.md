# 实际验证记录

验证日期：2026-11-15（Asia/Shanghai）  
运行环境：Ubuntu 22.04.5 LTS，Python 3.10.12

## 自动测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：16 项全部通过，耗时约 3.7 秒。

覆盖内容：

- 非法宽度、越界不可用格、未知约束、最小房间数超过可用格数：均返回对应 `E_xxx`，且不创建快照目录或输出文件。
- 缺少 `constraints` 字段或内存预算不足以容纳基础/候选状态：分别在输入与资源错误阶段拒绝，不创建快照或输出。
- 同一规格/种子重复求解：网格、房间、最终版本、候选尝试计数、各快照内容摘要完全一致。
- 并发数 1 与 4：最终网格、房间、候选尝试计数、回退计数一致。
- 约束边界主动中断：退出码 75、写入中断标记；续算从下一约束继续，最终结果与不间断求解一致。
- 删除中断标记：从已持久化最新快照恢复，最终结果一致。
- 真实 `SIGINT`：进程退出码 75、标记持久化，续算结果一致。
- 后序约束冲突触发回退：记录 `rooms -> conn` 冲突对，使用前序约束替代候选成功；中断后续算仍逐格一致。
- 模拟“替代快照已写入、清单尚未更新”的强杀窗口：按提交序号恢复较新的未登记快照，最终网格、版本与尝试计数仍与基线一致。
- 最新快照改写：`verify` 与 `--strict-digest` 均返回 `E_DIGEST`；默认续算安全回退到上一有效快照，最终结果一致。
- 删除快照 `digest`：`E_DIGEST`。
- 修改清单应用序号并重新计算清单摘要：`E_DIGEST`。
- 中断标记版本过期：归档旧运行并从头重做，最终结果一致。
- 规格种子变化：旧运行进入 `archive/runs/`，不覆盖旧快照，新结果与独立基线一致。
- 预检并发失败顺序：并发 1 与 4 的首个错误约束均为声明顺序中的第一个。

## 确定性验收脚本

命令：

```bash
python3 scripts/acceptance.py
```

报告写入 `docs/verification_report.json`，本次关键结果：

```text
ok = true
sample.seed = 20261115
baseline_version_id = 04530c214e14ed8de4e9966c86a3dde4e7500242509e5c6fb0566201e94524f7
baseline_grid_sha256 = 5f8273f5c06a64be41d8301cc2343a040774cb7a369b14a958d802ea0c5fe08e
baseline_snapshot_count = 5
baseline_candidate_attempts = 5
interrupt.exit_code = 75
interrupt.applied_count = 2
resume.source = interrupt_marker
resume.start_index = 2
resume.grid_equal = true
resume.final_version_equal = true
tamper.verify_code = E_DIGEST
tamper.strict_digest_code = E_DIGEST
tamper.fallback_start_index = 4
tamper.final_version_equal = true
conflict.code = E_CONFLICT
conflict.unresolved_constraints = ["spacing"]
conflict.output_created = false
```

## CLI 结果抽查

冲突样本命令：

```bash
python3 -m level_solver sample conflict conflict.json
python3 -m level_solver solve conflict.json \
  --snapshot-dir conflict-snapshots --output conflict-result.json
```

实际退出码 `3`，错误详情：

```json
{
  "code": "E_CONFLICT",
  "unresolved_constraints": ["spacing"],
  "conflict_pairs": [{"earlier": "rooms", "later": "spacing"}],
  "rollback_count": 0,
  "rollback_limit": 0,
  "applied_count": 1
}
```

`conflict-result.json` 未创建。

篡改样本命令：

```bash
python3 -m level_solver verify spec.json --snapshot-dir snapshots
```

改写最新快照网格但保留旧摘要后，实际退出码 `4`：

```json
{
  "ok": false,
  "error": {
    "code": "E_DIGEST",
    "message": "snapshot verification failed"
  },
  "exit_code": 4
}
```
