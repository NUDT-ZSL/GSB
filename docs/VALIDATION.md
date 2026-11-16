# 实际验证记录

验证日期：2026-11-17  
当前验证环境：Linux 容器，Python 3.11.0rc1（标准库运行；容器无 `pip`/NumPy、无 Tkinter）  
目标环境：Windows 本地 Python 3.11 + NumPy + 标准库 Tkinter

## 自动测试

```text
python3 -m unittest discover -s tests -v
Ran 36 tests in 0.83s
OK
```

覆盖范围：

- 目录唯一性、字段路径、声明行数、列类型和倾斜拒绝。
- 空白/大小写/别名/连接书写顺序规范化，非法查询特性。
- 计划枚举、稳定排序、定点同格决胜、顺序偏置。
- 四类样本查询的跨计划字节一致。
- 重复执行结果与稳定统计一致，耗时独立。
- LEFT JOIN 的行序和右侧 WHERE 语义。
- 版本单调、陈旧版本拒绝、快照整体替换、快照篡改。
- 三类文件级注入与结构化日志。
- 从无关工作目录以绝对路径启动 CLI。
- 两次独立验收报告/会话记录字节稳定。

## 端到端验收

```text
python3 run_cli.py acceptance
```

结果：

```json
{
  "all_anomaly_codes_as_expected": true,
  "all_cross_path_consistent": true,
  "final_param_version": 13,
  "query_count": 4,
  "refresh_converged": true,
  "result_bytes_are_compared": true,
  "snapshot_restored": true,
  "timings_are_observational": true
}
```

| 查询 | 选中计划 | 候选数 | 结果行数 | 跨路径 |
| --- | --- | ---: | ---: | --- |
| two_table_inner | `order=customers>orders\|ops=hash` | 4 | 9 | 一致 |
| three_table_inner | `order=orders>customers>order_items\|ops=hash,hash` | 20 | 9 | 一致 |
| left_customer_orders | `order=customers>orders\|ops=hash` | 2 | 9 | 一致 |
| three_table_products | `order=order_items>products>orders\|ops=hash,hash` | 20 | 8 | 一致 |

快照恢复：对保存前 4 个查询全部重新估算/执行，比较选中计划 ID、计划签名、结果摘要、行数和统计指纹；全部一致，恢复策略为 `full_replace`。

刷新突发：连续 12 次参数修改，参数版本从初始值单调推进到 `param_version=13`；旧票以 `E_STALE` 拒绝，最终只接受最新版本结果。

异常注入：

| 类型 | 结果 | 路径 |
| --- | --- | --- |
| 倾斜放大 | `E_SKEW` | `$.tables[0].rows` |
| 连接列类型不匹配 | `E_TYPE` | `$.queries[0].sql` |
| 快照参数版本缺失 | `E_SNAP` | `$.versions.param_version` |

## 稳定产物

```text
62daaaa4f0404a3bf4460e4bdf20b5614972e74e740b93c02c83c6037a8afb46  artifacts/acceptance/acceptance_report.json
d563d593ff4c5d1eb682115e05538323b1907332c39b9fecdd60b4ed751258d5  artifacts/acceptance/session_record.json
```

上述两个文件由第二次独立验收逐字节比较，均未变化。快照含观测耗时，不作为逐字节稳定产物比较。

## GUI 验证边界

当前容器 Python 没有 Tkinter，实际执行 `python3 run_gui.py` 得到：

```json
{
  "ok": false,
  "code": "E_GUI",
  "message": "Tkinter is unavailable in this Python installation: No module named 'tkinter'",
  "path": "gui.tkinter"
}
```

GUI 模块已通过 `py_compile`，其刷新控制由独立的 `RefreshGate`、请求序号和会话版本检查组成。Windows 官方 Python 3.11 自带 Tkinter，可直接用：

```powershell
C:\path\to\project\.venv\Scripts\python.exe C:\path\to\project\run_gui.py
```

## NumPy 说明

运行与测试核心完全不依赖 NumPy；`requirements.txt` 和 `tools/setup_windows.ps1` 会在 Windows 虚拟环境中安装 NumPy。当前容器没有 `pip`/`ensurepip`，也无管理员权限，因此未在本容器重复安装 NumPy。
