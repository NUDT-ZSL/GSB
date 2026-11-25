# 实际验证记录

验证日期：2026-11-26（Asia/Shanghai）。

## 环境

- Ubuntu 22.04.5 LTS（WSL 工作区记录为 `Ubuntu-22.04`）。
- 系统 Python：Python 3.10.12，NumPy 1.26.4，pytest 8.2.0，pip 26.3。
- 额外 Python 3.11：CPython 3.11.18，NumPy 2.4.6，pytest 9.2.0，pip 26.3。
- SQLite：Python 标准库 `sqlite3`。

Python 3.11 环境通过以下方式准备：

```bash
uv python install 3.11
uv venv --python 3.11 .venv311
. .venv311/bin/activate
python -m pip install numpy pytest
```

## 自动化测试

系统 Python 3.10：

```text
27 tests, 0 errors, 0 failures, 0 skipped
JUnit elapsed: 42.077 s
report: docs/py310-junit.xml
```

Python 3.11：

```text
27 tests, 0 errors, 0 failures, 0 skipped
JUnit elapsed: 45.774 s
report: docs/py311-junit.xml
```

复跑命令：

```bash
python -m pytest -q
. .venv311/bin/activate && python -m pytest -q
```

## CLI 固定样本烟测

```bash
demo=$(mktemp -d /tmp/diag-cli-XXXXXX)
python -m diagnostic_engine.cli run \
  --config "$PWD/samples/scenario.json" \
  --observations "$PWD/samples/observations.jsonl" \
  --workdir "$demo" --profile float64
python -m diagnostic_engine.cli verify 1 \
  --config "$PWD/samples/scenario.json" \
  --observations "$PWD/samples/observations.jsonl" \
  --workdir "$demo" --profile float64
```

实际结果：

```text
ok True windows 8 records 690
actions 48 verification True
query keys ['cross_expand', 'error', 'predict', 'preprocess', 'reduce', 'standardize']
verify ok True checks ['config_hash', 'cache_checksums', 'prediction_recompute', 'error_spectrum_recompute']
```

示例输出保存在：

- `docs/example-run.json`
- `docs/example-query.json`
- `docs/example-verify.json`

## 验收点映射

| 验收点 | 测试/证据 |
| --- | --- |
| 固定种子确定性 | `test_fixture_bytes_are_deterministic`、`test_pipeline_hashes_are_repeatable` |
| 输入与定义错误码 | `tests/test_error_codes.py` 覆盖 `E_BAD_INPUT`、`E_CROSS_DEF`、`E_MEM_LIMIT`、`E_STD_ZERO`、`E_INTERMEDIATE_DIM`、`E_DRIFT_MISSING`、`E_QUANTILE_ORDER`、`E_PRECISION_INVALID` |
| 增量诊断 | `tests/test_incremental.py` 覆盖全复用、降维变更、cross slot 变更、单变量变更后缀、窗口滑动 |
| 跨精度一致性 | `test_precision_profiles_share_structure_and_stay_close` |
| 强制终止恢复 | `test_force_kill_recovery_matches_uninterrupted_run` 使用真实 `SIGKILL` 自毁并比较完整窗口结果 |
| 未提交缓存清理 | `test_uncommitted_orphan_cache_is_removed_on_recovery` |
| 并发提交控制 | `test_concurrent_commit_returns_stage_busy_immediately` 验证非阻塞 `E_STAGE_BUSY` |
| 信号退出报告 | `test_catchable_signal_writes_exit_report` |
| 查询与重放 | `test_query_and_replay_match`、`test_replay_hash_is_content_stable` |
| stale 结果 | `test_stale_config_result_is_rejected` |
| 严格/容差分位比较 | `test_strict_and_tolerance_quantile_comparison`，误差谱同时输出观测值双策略比较 |

## 增量行为样例

降维候选从 `nested` 切换为 `pca3` 后，8 个窗口的动作计数为：

```text
preprocess      reused=8 recomputed=0
cross_expand    reused=8 recomputed=0
standardize     reused=8 recomputed=0
reduce          reused=0 recomputed=8
predict         reused=0 recomputed=8
error           reused=0 recomputed=8
```

修改窗口 2 中一个 `x03` 样本后：

```text
preprocess/predict: 1 window recomputed, 7 reused
error:              windows 2..7 recomputed (6), windows 0..1 reused (2)
```

这体现了在线漂移状态导致的后缀失效传播。
