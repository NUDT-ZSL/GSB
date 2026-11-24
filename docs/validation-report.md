# 实际验收结果

- 结论：全部通过
- Python：`3.11.18`；NumPy：`2.4.6`；平台：`Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.35`
- 固定种子：`20261125`；版本摘要：`sha256:5c6fd73f695810bd8e76af71bbfa1358e8912522bed00d26f4636bfda57e6772`
- 样本规模：13 骨、3 片段、1101 帧、18 块、5 个转移事件
- 单元测试：返回码 `0`，日志 `artifacts/acceptance/unittest.log`

## 增量闭包

| 步骤 | 算子 | 失效块 | 重算 | 复用 | 移植 | 最大姿态差 |
|---:|---|---|---:|---:|---:|---:|
| 0 | `update_key_value` attack | `[7, 8, 9]` | 3 | 15 | 0 | 0 |
| 1 | `add_key` attack | `[7, 8]` | 2 | 16 | 0 | 0 |
| 2 | `set_clip_duration` run | `[6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17]` | 12 | 6 | 12 | 0 |
| 3 | `update_transition_condition` t_idle_to_run | `[0, 1, 2, 3, 4, 5, 6, 7, 16]` | 9 | 9 | 0 | 0 |

## 恢复与持久化

- 可处理中断：完成 2 块后退出码 `75`，标记 `INTERRUPTED.json` 已写入。
- 同版本恢复：重算 16 块、复用 2 块，一致性 `True`。
- 摘要篡改：`E_DIGEST` 检出后仅重算 1 块，恢复一致性 `True`。
- 版本变化：归档 18 个旧块文件并全量重算，一致性 `True`。
- 真实 `SIGTERM` 子进程与恢复路径包含在 `tests/test_acceptance.py`；Windows 分支使用 `CTRL_BREAK_EVENT`。
