# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

环境：

- `Linux 6.18.33.2-microsoft-standard-WSL2 x86_64`
- Python `3.10.12`（要求 3.9+）
- SQLite `3.37.2`
- 仅使用 Python 标准库

## 1. 单元与集成测试

命令：

```bash
python3 -m unittest discover -s tests
```

实际结果：

```text
.......
----------------------------------------------------------------------
Ran 7 tests in 3.387s

OK
```

覆盖内容：

- 合并裁决与同时钟来源优先级；
- 观测批次重复提交幂等；
- 探测结论计数和重复探测零新增；
- 补丁依赖闭包与可复用规则；
- 在途窗口绑定窗口开始时的规则版本；
- 秒数、依赖、墓碑、观测序号和摘要等额外错误码；
- 重复规则、分片哈希不一致等错误码；
- `verify` 中的完整端到端、SIGKILL 恢复和多进程并行。

## 2. 可重复 CLI 自检

命令：

```bash
python3 -m cdn_engine.cli verify --workspace .verify-run
```

连续两次使用固定本地端口 `18080/18099` 运行，均得到：

```text
ok=True
check_count=30
conclusion_sequence_digest=sha256:b7ac3eebd924f81dc1e70941e789f60dc727d1aff8413556464273af40efe2f7
DIGEST_MATCH=True
```

该摘要覆盖按 `start_ms, node, rule, conclusion_id` 排序的全部结论码与结论摘要；两次独立工作区运行逐位一致。

30 项检查为：

1. `load_validates_sample`
2. `merge_rule_count`
3. `equal_clock_uses_source_priority`
4. `v1_relative_time_conversion_recorded`
5. `probe_counts_all_observations`
6. `conclusions_committed`
7. `expected_verdicts`
8. `origin_refusal_distinguished`
9. `hit_verdict`
10. `digest_mismatch_verdict`
11. `connection_refused_verdict`
12. `no_observation_verdict`
13. `origin_headers_digest_and_version_output`
14. `conclusion_sequence_bitwise_repeatable`
15. `dependency_closure_path`
16. `closure_recompute_windows`
17. `patch_idempotent`
18. `package_full_replace`
19. `package_rollback_rejected`
20. `sigkill_fault_injection`
21. `resume_replays_only_unfinished`
22. `recovery_no_duplicate_conclusions`
23. `package_switch_sigkill`
24. `package_pointer_unchanged_before_recovery`
25. `package_staging_aborted_by_recovery`
26. `package_pointer_stable_after_recovery`
27. `parallel_node_processes_exit_zero`
28. `parallel_no_duplicate_counts`
29. `duplicate_rule_rejected`
30. `shard_hash_mismatch_rejected`

## 3. 关键业务结果

固定样本的时钟原点为 `1699999980000`，与 60 秒窗口边界对齐；最终探测时钟为 `1700000100000`。

首次完整探测后：

- 输入观测：7 条；
- 两个窗口周期内的窗口/结论：42 个；
- `CACHE_HIT_VERIFIED=5`；
- `DIGEST_MISMATCH=1`；
- `ORIGIN_CONNECTION_REFUSED=1`；
- 其余为 `NO_OBSERVATION`。

这证明摘要不一致、回源连接拒绝和窗口内无观测是三种不同结论。

补丁 `patch-origin-catalog-generation-bump` 的闭包为：

```text
origin-catalog
  -> catalog-list
  -> catalog-page
```

精确受影响规则为 `origin-catalog`、`catalog-list`、`catalog-page`；`api-auth`、`shared-config`、`origin-auth`、`edge-personalization` 等规则的既有结论标记为可复用且不被改写。

## 4. 强制终止恢复

探测故障注入：

```bash
python3 -m cdn_engine.cli probe \
  --state var/crash.db --clock 1700000100000 --crash-after-obs 1
```

实际进程被 SIGKILL 终止，shell 观测到退出码 `137`。终止后数据库中立即可见：

- `processed_observations=1`；
- `windows.open=1`；
- 逻辑时钟水位已提交。

执行：

```bash
python3 -m cdn_engine.cli resume --state var/crash.db
```

实际恢复结果：

```text
processed_new=6
skipped_duplicates=1
open_windows_remaining=0
processed_observations=7
conclusions=42
```

再次 `resume` 时 `processed_new=0`，不会重复计入结论。

规则包切换也有提交前 SIGKILL 注入点。强杀后活动视图指针仍为旧版本且存在一个 `staging` 操作；`resume` 将该操作标记为 `aborted_recovered`，指针保持不变。

## 5. 多进程并发

验证脚本同时启动三个 CLI 进程，分别处理 `edge-a`、`edge-b`、`edge-offline`。实际结果：

```text
parallel_node_processes_exit_zero=True
processed_observations=7
conclusions=42
```

节点文件锁、全局视图锁和 SQLite 唯一约束共同保证没有重复观测计数或并发指针覆盖。

## 6. 推荐人工复验命令

```bash
python3 -m cdn_engine.seed --out samples/demo
python3 -m cdn_engine.cli verify --workspace .verify-run
python3 -m unittest discover -s tests
```

如需观察真实 HTTP 回源服务，可另开终端：

```bash
python3 -m cdn_engine.cli serve-origin --port 18080
```

然后按 `README.md` 的快速开始执行。所有访问目标均为 `127.0.0.1` loopback。
