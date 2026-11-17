# 验证结果报告（可复现）

运行环境：Python 3.11（标准库 sqlite3）；NumPy 2.4.6 可选路径已交叉验证；
Tkinter 在当前无头 WSL 不可用，可视化自动降级为文本表格（Windows 官方 Python 自带 Tk）。

## basic
- 决策计数：{'E_OK': 7, 'E_AUTH': 3, 'E_LIMIT': 2}
- 三路径比较：一致=False，分歧数=1，比较请求=9
  - seq=12 ts=1100 边界=window_alignment 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_OK'}
- 账本重放：{'batches_checked': 3, 'status': 'match', 'versions': [[1, 1]]}
- 自适应选择：inventory -> token_bucket（tie=True；完整理由见 workdir/basic/snapshots/selection.json）
- 自适应选择：orders -> token_bucket（tie=True；完整理由见 workdir/basic/snapshots/selection.json）

## boundary
- 决策计数：{'E_OK': 11, 'E_AUTH': 0, 'E_LIMIT': 0}
- 三路径比较：一致=False，分歧数=7，比较请求=11
  - seq=3 ts=150 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
  - seq=4 ts=300 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
  - seq=5 ts=500 边界=window_alignment 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_OK'}
- 账本重放：{'batches_checked': 3, 'status': 'match', 'versions': [[1, 1]]}
- 自适应选择：edge -> token_bucket（tie=True；完整理由见 workdir/boundary/snapshots/selection.json）

## signature
- 决策计数：{'E_OK': 5, 'E_AUTH': 1, 'E_LIMIT': 1}
- 三路径比较：一致=False，分歧数=1，比较请求=6
  - seq=4 ts=400 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
- 账本重放：{'batches_checked': 2, 'status': 'match', 'versions': [[1, 1]]}
- 自适应选择：payments -> token_bucket（tie=True；完整理由见 workdir/signature/snapshots/selection.json）

## large
- 决策计数：{'E_OK': 1440, 'E_AUTH': 0, 'E_LIMIT': 0}
- 三路径比较：一致=True，分歧数=0，比较请求=1440
- 账本重放：{'batches_checked': 15, 'status': 'match', 'versions': [[1, 1]]}
- 自适应选择：telemetry -> token_bucket（tie=True；完整理由见 workdir/large/snapshots/selection.json）

## bursty
- 决策计数：{'E_OK': 720, 'E_AUTH': 0, 'E_LIMIT': 0}
- 三路径比较：一致=False，分歧数=120，比较请求=720
  - seq=601 ts=600 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
  - seq=602 ts=600 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
  - seq=603 ts=600 边界=refill_truncation 决策={'token_bucket': 'E_OK', 'sliding_window_counter': 'E_LIMIT', 'sliding_window_log': 'E_LIMIT'}
- 账本重放：{'batches_checked': 8, 'status': 'match', 'versions': [[1, 1]]}
- 自适应选择：burst -> token_bucket（tie=True；完整理由见 workdir/bursty/snapshots/selection.json）

## tcp
- 决策计数：{'E_OK': 7, 'E_LIMIT': 2, 'E_AUTH': 3}
- 回环端口：127.0.0.1:42978，模拟客户端收到 12 条响应

## crash_recovery
- 账本重放：{'batches_checked': 3, 'status': 'match', 'versions': [[1, 1]]}
- 强制终止恢复：未知批次=[4] 完整批次=[1, 2, 3] 重放=match

## evolution
- 策略整体替换：{'new_version': [2, 2], 'replaced_version': [1, 1], 'affected_routes': ['inventory', 'orders'], 'cache_entries_cleared': 0, 'limiter_state': 'wholesale_replaced'}

## injections
- 注入 corrupt_policy -> E_SPEC：invalid JSON: Extra data
- 注入 token_version_conflict -> E_VERSION：token table version 1 regresses from active version 2
- 注入 zero_window -> E_LIMIT：window_ms must be > 0

## invalid_spec
- E_SPEC routes_duplicate.json 行 5：duplicate route name 'dup'
- E_SPEC routes_bad_window.json 行 5：route 'broken' field window_ms must be a positive integer
- E_SPEC requests_non_monotonic.ndjson 行 2：ts_ms 99 is earlier than previous 100; timestamps must be monotonic

## 单元测试
- `python -m unittest discover -s tests`：36 个用例全部通过（无 NumPy 时跳过 1 个可选用例）。
- NumPy 模式下同一命令 36 个用例全部通过，标量/向量化工作量估计逐值相等。

## 复现命令（Windows）
```bat
python tools\make_samples.py
scripts\run_tests.cmd
scripts\clean.cmd
scripts\run_demo.cmd
```