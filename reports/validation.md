# 实际验证记录

## 解释器与依赖

```text
Python 3.11.18
numpy 2.4.6
pytest 9.2.0
```

## pytest

命令：

```text
.venv311/bin/python -m pytest -q
```

结果：

```text
31 passed
```

覆盖范围：

- 注册与尝试严格解析、重复标识、类型/小数位/摘要/动态码格式拒绝；
- v1 观测时钟、v2 分片与 SQLite 合并、墓碑覆盖和策略版本变化；
- 指纹确定匹配、阈值跨越歧义、确定不符；
- TOTP 确定通过、确定拒绝、窗口边界歧义；
- 双条件三类结论分离统计；
- 票据重放两次尝试回溯；
- 用户/因子撤销传递闭包、依赖环检测；
- 票据续期窗口和代数上限；
- 预览不提交状态、前置失败不改账本；
- 批量并发度/到达顺序一致性、队列上限、真实墙钟超时；
- 策略版本/布局变化整体重建；
- 检查点半写恢复、损坏新检查点回退；
- 文件锁冲突 `E_LOCK`；
- 原子报告封印和临时文件清理；
- 真实子进程强制终止后的恢复且无退出报告。

## 固定种子演示

命令：

```text
.venv311/bin/python -m mfa_guard --root . demo --seed 20261125
```

关键结果：

```text
policy_generation = 3
preview_status = APPROVED
preview_matches_batch = true
adjudicated = 8
approved = 3
rejected = 3
indeterminate = 2
total_records = 13
prevalidation_rejected = 2
parse_rejected = 3
E_FP_AMBIGUOUS = 1
E_OTP_AMBIGUOUS = 1
E_TICKET_REPLAY = 1
```

报告：

- `reports/decision_report.json`

## 自检

命令：

```text
.venv311/bin/python -m mfa_guard --root . verify --seed 20261125
```

结果：

```text
scenario_expectations = passed
batch_order_and_concurrency_consistency = passed
interruption_recovery = passed
checkpoint_lock_conflict = passed
overall = passed
```

半写恢复摘要：

```text
status = RECOVERED_HALF_WRITE
half_write_detected = true
restored = true
final_digest == baseline_digest
```

自检报告：

- `reports/verification_report.json`

