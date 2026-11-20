# 验证记录（本地确定性样本）

环境：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3、Linux 容器；同样的命令在
Windows + Python 3.10/3.11 + NumPy 下可直接运行（无平台特定 API）。

## 1. 自动化测试

命令：

```bash
python -m pytest tests/ -q
```

结果：**29 passed**。覆盖映射：

| 测试文件 | 覆盖业务要求 |
| --- | --- |
| `test_quantize.py` | 要求 1：四类清单/形状/scale/zero_point 错误码、量化确定性与误差 |
| `test_reproducibility_clean.py` | 要求 1、2：两次运行逐字节一致、层间输入=上层输出、重算顺序不影响预测 |
| `test_recovery.py` | 要求 3、4、5：单调原子进度点、子进程强制终止恢复、损坏检测、回滚拒绝、整体替换 |
| `test_concurrency_budget.py` | 要求 6：非阻塞锁、层数/内存/缓存硬上限、流式降级且结果不变 |
| `test_faults.py` | 要求 7：四阶段参数注入、归因记录与下游降级 |
| `test_consistency.py` | 要求 8：层集合、量化往返、重算/复用划分、独立重算预测 |

> 说明：强制终止测试在**子进程**中运行 `python -m qpipe.cli ... run --kill-after-layer ...`，
> 子进程以退出码 `137` 硬终止且不产出退出报告，pytest 主进程随后执行恢复断言。

## 2. 端到端验收

命令：

```bash
python scripts/run_demo.py
```

最近一次运行 `acceptance_report.json` 中 `all_ok = true`，九项检查全部 `PASS`：

| 检查 | 结果 | 关键观测 |
| --- | --- | --- |
| `deterministic_rerun_byte_identical` | PASS | 两个独立工作目录预测/激活/量化参数校验和完全一致 |
| `forced_kill_resume_byte_identical` | PASS | 子进程退出码 137，中断 HEAD 停在 `calibrate`，恢复后与完整运行逐字节一致 |
| `revision_downstream_only_recompute` | PASS | 变更第 2 层时仅 `[2,3,4]` 重算，`[0,1]` 复用 |
| `rollback_rejected` | PASS | 旧修订提交返回 `E_SNAPSHOT_ROLLBACK` |
| `cache_corruption_detected` | PASS | 篡改缓存条目的 scale 后读取返回 `E_CACHE_CORRUPT`（层号 3） |
| `non_blocking_stage_lock` | PASS | 第二次获取同阶段锁立即返回 `E_STAGE_BUSY`，不阻塞 |
| `fault_attribution_all_stages` | PASS | validate/quantize/calibrate/infer 四阶段均产生带码与层号的记录 |
| `consistency_self_check` | PASS | 层集合、量化往返、重算/复用划分、独立预测重算全部通过 |
| `resource_observations_present` | PASS | 含按层估算内存与每阶段实测耗时 |

一次代表性运行的阶段实测耗时（毫秒，本地 `time.perf_counter` 观测，随机器波动）：

```
validate≈25.3  quantize≈30.0  calibrate≈31.6  infer≈28.5
```

5 层（4 个 8×8 + 1 个 8×2）反量化权重估算驻留内存合计 **1088 字节**
（按 float32、4 字节/元素估算）。该数值仅为预算观测，不代表真实内存上限或隔离。

## 3. CLI 手工抽查

- `--faults scripts/faults_example.json run`：四条注入分别落在四个阶段，
  `degraded_mode=true`，降级类别包含 `degraded_<stage>` 与
  `degraded_<stage>_upstream`（携带触发它的上游业务码）。
- 预算文件 `{"max_layers_per_stage":2}`：`run` 返回 `E_RESOURCE_LIMIT`。
- `interactive`：菜单 1 显示 `stage=infer progress_version=4`，菜单 2 显示逐层
  量化误差（max_abs_error、rmse、snr_db）。
- `kill-demo` 与 `revision-demo` 子命令输出的字节一致性布尔位均为 `true`。

## 4. 复现实验步骤

```bash
cd qpipe
pip install -r requirements.txt
python -m pytest tests/ -q
python scripts/run_demo.py
```

所有输入由固定种子（默认 `20261121`）在本地生成，重复执行得到相同校验和，
无网络、GPU、容器或外部服务依赖。
