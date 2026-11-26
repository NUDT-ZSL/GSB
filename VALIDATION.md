# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）

环境：

- Python 3.10.12
- NumPy 2.0.0
- pytest 8.2.0
- SQLite Python 标准库
- 未使用 Docker，未访问网络或未授权目标

## 依赖确认

```text
$ python3 -m pip install numpy
Requirement already satisfied: numpy ... (2.0.0)
```

## 固定种子样本

```text
$ python3 seed_data.py
seed=20261127 mentions=21 candidates=16
```

生成 13 个句子行、21 个 mention、16 条固定候选，以及 v1 JSON、v2 分片 JSON 和 `rules_meta.db`。

## 一键验收

```text
$ python3 run_acceptance.py
status=ok
single_full_pipeline=0.7366s
total_acceptance=2.5555s
counts=21 candidates / 7 chains / 2 conflicts / 12 traces
determinism.byte_identical=true
paths.ok=true
incremental.self_check_ok=true
recovery.signal_mode=handleable_signal
recovery.force_mode=forced_termination
recovery.dirty_mode=dirty_checkpoint
recovery.chains_match_clean_run=true
concurrency.busy_codes=['E_STAGE_BUSY']
```

完整机器可读结果保存在 `validation/acceptance_report.json`。验收脚本额外执行两次独立目录运行并比较规范报告字节，结果一致。

## 单元与集成测试

```text
$ python3 -m pytest --disable-warnings
........................ [100%]
24 passed in 4.61s
```

完整输出保存在 `validation/pytest.txt`。

## 已覆盖的验收点

- v1/v2 读取、v1→v2、fold/discard v2→v1 均通过内部规范字节校验。
- 未知版本返回 `E_VERSION_UNSUPPORTED`；缺字段和 SQLite 时间戳倒退返回 `E_FORMAT_CORRUPT`。
- 精确路径与近似分桶路径的候选分数、链成员、边和置信度一致。
- 近似路径输出桶过滤依据和零权重剪枝特征明细。
- 每个评分总分可由组件贡献独立重算。
- 冲突按总分降序、规则代数升序裁决，并保留落败候选和首个差异。
- 增量 delta 重算 8 个变更文档 pair，复用 4 个未受影响闭包缓存；3 个受影响闭包重算。
- 规则代数从 2 提升到 3 的测试验证仅规则相关 pair 进入失效集合。
- trace 重放重建 7 条链和 12 条边；旧缓存键被使用时返回 `E_STALE_LINK`。
- SIGTERM 风格边界停止、`os._exit(137)` 强杀恢复、脏检查点三类情形均有测试。
- 非阻塞提交锁竞争返回 `E_STAGE_BUSY`。
- 重复链注册返回 `E_DUPLICATE_CHAIN`；人为制造双路径分歧时自检返回 `E_INCONSISTENT`。

## 性能

单次完整管线实测约 0.74 秒；包含种子、双确定性副本、迁移、信号恢复、强杀子进程恢复、脏检查点和并发锁的一键验收约 2.56 秒，均低于题面 12 秒目标。完整 pytest 约 4.61 秒。
