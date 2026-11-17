# 实际验证记录

验证日期：2026-11-17

工作区：`/home/zsl/feedback/uid85`

## 当前执行环境

- Python：`3.11.0rc1`
- `sqlite3`：`3.37.2`
- 运行器系统 Python 默认未安装 `tkinter`；使用本机已有的 Tk 8.6/Tcl 8.6 运行库副本完成真实开窗冒烟
- 运行器系统 Python 未安装 NumPy；使用本机已有的 NumPy 2.4.6 站点包通过 `PYTHONPATH` 验证 NumPy 分支
- 未使用 Docker、容器或网络服务

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 30 tests in 1.590s
OK
tkinter unavailable: ModuleNotFoundError: No module named 'tkinter'
```

NumPy 分支命令：

```bash
PYTHONPATH=/home/zsl/feedback/uid20/.venv/lib/python3.11/site-packages \
  python3 -m unittest discover -s tests -v
```

结果：

```text
numpy 2.4.6
NUMPY_AVAILABLE True
Ran 30 tests in 1.428s
OK
```

两轮测试均通过，覆盖纯整数回退和 NumPy 辅助分支。测试中包含：

- `E_DECL`：重复服务名、负负载、非整数负载、零预算、零并发、零耗时、最小值大于最大值、JSON 损坏、缺失字段路径；
- `E_DEMAND`：缺少必要输入、请求分钟越界；
- `E_ALLOC`：接受量与容量不一致、分钟预算超限；
- `E_REPLAY`：账本篡改后首个差异定位、声明版本不一致、负载序列不一致；
- 版本回滚到旧内容时仍生成新版本；
- 陈旧声明版本快照不得恢复为当前视图；
- 并行估算输入乱序结果一致；
- 强制中断后只恢复完整前缀，未决分钟未知且无退出报告；
- 恢复续跑完成后全量重放；
- 整轮快照恢复与逐字段比较。

## 编译与 CLI

```bash
python3 -m compileall -q model_service_workbench scripts tests
```

结果：成功。

```bash
python3 -m model_service_workbench validate --declaration samples/cluster.json
```

结果：返回 `ok=true`，3 个服务、8 分钟、预算 10，字段校验通过。

## 三场景演示

命令：

```bash
python3 scripts/demo.py
```

结果：`result=OK`，稳定输出位于 `artifacts/demo_output/demo_report.txt`。

同一脚本连续运行两次后，对输出文件执行 `diff -u`，返回 0，演示摘要逐字节稳定。

关键实测值：

- 基准版本 1，运行 `run-v1-0001`，8/8 分钟完成，重放 8 分钟逐项一致。
- 分钟 4 全局预算 10 已满：alpha 需求 5 -> 配额 4；beta 需求 7 -> 配额 6 -> 全局预算 3；gamma 需求 4 -> 配额 3。beta 拒绝量 36，符合最终容量 `3 * 9 = 27`。
- 修改 beta 并发上限 9 -> 6 后，自动生成版本 2；差异路径为 `$.services["beta"].concurrency_per_replica`，受影响服务 `beta`，受影响分钟 `[0,7]`，旧运行 `run-v1-0001` 标记 `is_current=false`、`is_stale=true`。
- 版本 2 运行 `run-v2-0002`，8/8 分钟完成，全量重放一致。
- 保存整轮快照 `snap-v2-0001`，恢复为新运行 `run-v2-0003`；恢复 8 分钟，`decision_fields_exact_match=true`，恢复后重放 8 分钟一致。

演示末尾输出：

```text
result=OK
verified=baseline_full,version_invalidation,version_rerun,budget_clipping,snapshot_restore,field_by_field_replay
```

## 强制中断恢复验证

由 `tests/test_ledger_replay.py::test_forced_interruption_recovery_and_resume` 验证：

- 强制停止前仅提交分钟 0、1、2；
- 重开账本后恢复 3 个完整分钟；
- `pending_minutes` 和 `unknown_minutes` 均为 `[3,4,5,6,7]`；
- `first_unknown_minute=3`、`requires_rerun=true`、`exit_report_available=false`；
- 先重放 3 个已完成分钟，再从未知分钟续跑；
- 续跑后 8/8 分钟完成，并再次全量重放通过。

## Tkinter 工作台验证

默认系统 Python 缺少 Tkinter 时：

- `model_service_workbench.ui` 仍可安全导入；
- `python -m model_service_workbench gui --check` 返回退出码 2，并输出明确环境错误；
- `compileall` 覆盖 UI 源码。

使用本机已有的 Tk 8.6/Tcl 8.6 运行库后，`gui --check` 输出 `tkinter available` 并返回 0。`python3 scripts/gui_smoke.py` 的真实窗口冒烟结果：

- 初始空视图 0 行；
- 推进 1 分钟后 Treeview 为 3 行，状态为 `run-v1-0001 | 1/8 | running`；
- 再推进 1 分钟后为 6 行；
- 全量推进后为 24 行（8 分钟 × 3 服务）；
- 预算曲线画布生成 42 个元素；
- 保存快照后列表显示 `snap-v1-0001 | v1 | 8 分钟`。

Windows 官方 Python 3.11+ 通常随安装包提供 Tcl/Tk。部署机器上应先执行：

```powershell
python -c "import tkinter; print(tkinter.TkVersion)"
```

成功输出 Tk 版本后再启动 `python scripts\gui.py`。
