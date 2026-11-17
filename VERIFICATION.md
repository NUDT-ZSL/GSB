# 实际验证结果

验证环境：Python 3.12.13（uv 安装的独立发行版，工作区 `.venv`），
NumPy 2.5.5，pytest 9.2.0。全部在本地离线完成。

## 自动化测试

```
$ .venv/bin/python -m pytest
................................................                         [100%]
48 passed in 3.02s
```

覆盖：解析拒绝码与条目序号、冲突/遮蔽/依赖环检测、声明顺序无关性
（排列组合重放）、并发度 1/2/4/8 结论等价、任务顺序稳定键归一化、
摘要与明细交叉自洽、队列饱和拒绝（E3001）、实测超时（E3002）、实测
内存超限（E3003）、批次预算耗尽（E3004）、崩溃-恢复等价、策略变更
整体重建（E4002）、检查点结构版本不匹配（E4001）、损坏检查点不恢复、
原子写入无半写文件、锁冲突（E5001）、并发写串行化、输入文件哈希不变。

## 场景实测（CLI 退出码）

| 场景 | 命令要点 | 退出码 | 结果 |
| --- | --- | --- | --- |
| clean | 默认配置 | 0 | 无发现，4/4 任务成功 |
| conflict | 默认配置 | 2 | E2001 冲突 (R-101, R-102) @ api.gateway |
| shadow | 默认配置 | 2 | E2002 遮蔽 (R-201→R-202, R-201→R-203) |
| cycle | 默认配置 | 2 | E2003 依赖环 [R-301, R-302, R-303] |
| invalid | 默认配置 | 1 | 6 条拒绝记录，含条目序号 1–6 与 E1001–E1006 |
| timeout | `--task-timeout 0.2` | 4 | slow.lane 实测 800ms → E3002；fast.lane 成功 |
| memory | `--task-memory-limit 8MiB` | 4 | big.job 实测分配 33,560,168 B → E3003；small.job 成功 |
| queue_full | `--concurrency 1 --queue-limit 1` | 4 | 8 任务中 5 个入队被拒（E3001），3 个成功，无静默丢弃 |
| concurrency | `--concurrency 1/2/4/8` | 2 | 四种并发度下 findings、任务顺序、摘要完全一致（8 冲突 / 12 遮蔽） |

## 中断恢复演示

```
$ .venv/bin/python scripts/demo_recovery.py
phase 1: killed after 3 checkpointed tasks
phase 2: resumed, exit=2, summary={'failed': 0, 'not_run': 0,
         'rejected_queue': 0, 'resource_limited': 0, 'succeeded': 6, 'total': 6}
recovery result identical to uninterrupted run: OK
conflict found: {'code': 'E2001', 'rules': ['R-900', 'R-910'], 'target': 'zone.0'}
```

- 强制终止后不生成退出报告（`report.json` 不存在），仅从最后一个完整
  检查点恢复。
- 恢复后结论、任务顺序、摘要与不间断运行逐项相等。
- 策略文件变更后 `--resume` 触发 E4002 并整体重建（见
  `tests/test_recovery.py::test_policy_change_forces_full_rebuild`）。

## 输入不可变性

`tests/test_cli.py::test_input_files_are_not_modified` 对 policies.json 与
tasks.json 在运行前后做 SHA-256 比对，哈希不变。
