# 实际验证记录

验证环境：Linux/WSL 本地 Python 3.11.0rc1，标准库 SQLite 3.37.2；当前验证容器未安装 Tk，因此 GUI 完成语法编译与导入验证，实际窗口需在带 Tk 的 Windows Python 上运行。

## 自动测试

命令：

```text
python3 -m unittest discover -s tests -v
```

结果：`Ran 10 tests in 2.031s`，`OK`。

覆盖计划规范化、拒绝子查询/聚合、IN/OR/范围、确定性灌入与回滚、估计确定性与缺失统计、真实计数与零值哨兵、热力整替与版本对比、`E_VERSION`、快照恢复/缺失统计（含无历史计划场景）、三类异常和日志重复性。

## 端到端实验

命令：

```text
python3 experiment.py --out reports
```

结果：30/30 检查通过，退出码 0。

关键观测：

- 数据版本：`dv_536d45df54fd3314771d429a`
- 统计 v1：`sv_3671274f0cac0df85372a0b4`（4 桶、0.5 采样）
- 统计 v2：`sv_78b127fa0177eca06e1746da`（12 桶、1.0 采样、空值覆盖）
- 计划 ID：`lp_edd8e87108adde3194c2fd89`
- v2 根估计/真实：`11 / 14`
- 主要偏差算子：`op_003`，偏差分数 `2143`
- v1→v2 变化算子：2 个（JOIN 与 FILTER）
- 快照重开恢复：3 条历史计划重放一致
- 异常码：`E_SKEW`、`E_STATS`、`E_REPLAY`
- 重复注入事件 ID 分别一致，数据版本未改变

详细结果见 `reports/experiment_report.md`、`reports/experiment_report.json` 和 `reports/anomaly_logs.json`。

连续两次运行实验后，对三份报告文件做 SHA-256 比较，结果完全一致，确认报告可复现。

## 命令编译检查

```text
python3 -m py_compile $(rg --files -g '*.py')
```

结果：成功。

## Windows 运行提示

在 Windows cmd.exe 中执行：

```bat
python -m unittest discover -s tests -v
python experiment.py --out reports
python -m sqlplan_lab --db lab.sqlite3 gui
```

或直接运行 `run_experiment.bat` / `run_gui.bat`。输出目录只需当前用户具备写权限。
