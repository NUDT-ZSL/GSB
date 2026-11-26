# 验证记录

环境：Linux（WSL Ubuntu），Python 3.10.12，numpy 2.0.0，无网络/GPU/容器。
依赖安装（离线环境）：`pip install numpy`；`sqlite3` 使用标准库。

## 1. 复现步骤

```bash
pip install -r requirements.txt
python3 scripts/generate_samples.py --out data/sample
python3 -m unittest discover -s tests -t .
python3 examples/run_demo.py
```

## 2. 单元/集成测试结果

`python3 -m unittest discover -s tests -t .` 实测：

```
Ran 39 tests in 0.5s 级别
OK
```

覆盖映射：

| 测试文件 | 覆盖要求 |
| --- | --- |
| `tests/test_ingestion.py` | 要求 1：排序、四类校验、确定性边界与校验和 |
| `tests/test_transforms.py` | 要求 2：float64 两条路径位级一致、轨迹可追溯、归约确定 |
| `tests/test_precision.py` | 要求 3：三档同语义、同边界、同顺序 |
| `tests/test_ledger.py` | 要求 4：四级误差、放大比、量化边界、容差错误码 |
| `tests/test_invalidation.py` | 要求 5：三类变更失效闭包、重算/复用分区 |
| `tests/test_snapshots.py` | 要求 6：v1/v2 迁移、墓碑丢弃、回退保护 |
| `tests/test_recovery.py` | 要求 7：检查点单调、中断恢复账本逐字节一致 |
| `tests/test_concurrency_consistency.py` | 要求 8：E_STAGE_BUSY、账本独立复算、E_INCONSISTENT |
| `tests/test_memory_determinism.py` | 内存预算守卫、跨运行与样本重生成逐字节一致 |

## 3. 端到端示例结果

`python3 examples/run_demo.py` 实测输出：

```
tolerance_raised: true        # 严格定点容差触发 E_TOLERANCE_EXCEEDED
recovery_byte_identical: true # 中断恢复后账本与无中断运行逐字节一致
rollback_blocked: true        # v2 -> v1 回退触发 E_SNAPSHOT_ROLLBACK
stage_busy: true              # 并发提交后到者立即 E_STAGE_BUSY
self_check_ok: true           # 账本可由记录参数独立复算、分区正确
```

完整报告见 `data/demo_output/demo_report.json`。两次完整运行的 36 个账本
文件经 SHA-256 比对全部逐字节一致；样本重新生成后状态 `.npy` 哈希不变。

## 4. 误差诊断实测（任务 t00，scale=100）

| 精度 | 初始校验 abs | 分块变换 abs | 跨块归约 abs | 首个偏差步骤 | 主导来源 |
| --- | --- | --- | --- | --- | --- |
| float64 | 0 | 0 | 0 | 无 | — |
| float32 | 4.84e-08 | 9.94e-08 | 1.36e-07 | 1 | cross_reduce |
| fixed | 4.63e-03 | 1.33e-02 | 5.91e-02 | 1 | cross_reduce |

定点档放大比实测：分块变换 2.86、跨块归约 4.46，体现量化误差经多步迭代
与归约逐级放大；float64 参考路径四级误差均为 0，符合“参考真值”定义。

## 5. 失效闭包实测

- revision 提升（t03 起）：重算 `{t03,t04,t05}`，复用 `{t00,t01,t02}`。
- 代数提升：全部 6 个任务进入失效闭包并重算。
- block_size 变化（t02 由 4 改 6）：闭包 `{t02,t03,t04,t05}`，其中仅 t02
  内容键改变需重算，其余后缀命中已提交缓存而复用。

三类计划均通过“重算集 ∩ 复用集 = ∅ 且并集 = 全部任务”的自检。

## 6. 内存预算实测

对 t00（dim=12，float64，4 步）构建账本时高水位约 592 字节（含参考轨迹
480 字节与分块工作区）；预算设为 64 字节时立即返回
`E_BUDGET_EXCEEDED`，正常预算退出后已用字节归零。

## 7. 已知边界

- 容差按精度档分别配置；定点 scale=100 的固有量化误差在百分位量级，
  正常容差据此设定，严格容差仅用于演示错误码路径。
- 为保证显式 Jacobi 语义，分块路径每步保留完整旧状态快照；块窗口预算
  与状态驻留预算分别记账，高水位可在报告中复核。
