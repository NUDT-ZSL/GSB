# 实际验证记录

验证时间：2026-11-26（Asia/Shanghai）。

## 环境

- Python `3.10.12`
- numpy `1.26.4`
- 标准库 sqlite3 运行时 `3.37.2`
- pytest `8.2.0`
- 未使用 Docker 或外部服务；所有对象均为本地固定种子生成。

## 复现命令

```bash
cd tierflow-lab
python3 scripts/generate_samples.py
bash scripts/acceptance.sh
```

`acceptance.sh` 使用独立的 `state/acceptance.db` 与 `state/acceptance-crash.db`，并把每次 CLI JSON 输出写入 `validation/`。

## 自动化测试

实际结果：

```text
11 passed in approximately 1.2s
```

覆盖：

- 固定种子样本与摘要/字节数校验；
- v1/v2 合并、v1 相对秒舍入、同钟来源优先级和墓碑；
- 定点代价误差链与精度敏感对象对；
- 完整调度、幂等重复执行和 `verify`；
- `within_budget_unstarted`、`started_failed`、`voluntarily_abandoned` 三类结局；
- `E_CLOCK_ROLLBACK=2001` 与 `E_POLICY_ROLLBACK=3001`；
- 真实 `SIGKILL` 后的 `claimed` 恢复；
- 真实 `SIGKILL` 后的 `committing` 半提交恢复；
- 三个 CLI 进程并发迁移同一轮；
- judge 规范化 JSON 逐位重复。

## 一键验收摘要

`validation/summary.txt` 的实际内容：

```text
acceptance_ok=true
policy_rollback_exit=2; clock_rollback_exit=2
merge_decision=full_replace
merge_noop=noop
append_decision=incremental_append
rollback_code=E_POLICY_ROLLBACK
clock_code=E_CLOCK_ROLLBACK
migration_status_counts={"abandoned": 3, "succeeded": 5}
migration_outcome_counts={"succeeded": 5, "voluntarily_abandoned": 3}
precision_pair=obj-0006/obj-0007
precision_cost_difference_scaled=256
precision_tolerance_scaled=1000
verify_ok=true
resume_recovered=1
resume_status_counts={"abandoned": 3, "succeeded": 5}
resume_watermark=5
```

强制终止命令由脚本内部 `kill(SIGKILL)` 触发，shell 记录：

```text
crash_exit=137
```

恢复后 5 个任务成功、3 个预算放弃，迁移水位恰为 5；再次 resume 不增加水位。

## 逐位一致性

两个全新 SQLite 状态库执行相同 `load`、`merge`、`judge`，输出哈希一致：

```text
d55c8ded7f42c418dba6b68e835ba3829dd5731f14b9f8b4cd121b6009724ccd  validation/judge-repeat-a.json
d55c8ded7f42c418dba6b68e835ba3829dd5731f14b9f8b4cd121b6009724ccd  validation/judge-repeat-b.json
JUDGE_REPEAT_BYTE_IDENTICAL
```

## 多进程结论

自动化测试启动 3 个 `migrate --workers 1 --task-delay 0.2` 进程处理同一个 `round_id`。最终数据库检查确认：

- `(policy_version, object_key)` 无重复任务；
- 无容量超扣；
- `migration_committed=5`；
- `verify_project(...).ok is True`。

各进程退出时可能观察到其他进程仍有 `running` 任务，因此单进程 stdout 的瞬时状态计数不承诺相同；所有进程结束后的 `status` 与 `verify` 是确定性最终结果。
