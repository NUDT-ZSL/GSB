# 运行手册（Windows / 本地）

## 环境准备

1. 安装 Python 3.11+（Windows 官方安装包勾选 tcl/tk，默认包含 Tkinter）。
2. 解压或克隆项目，无需 `pip install` 任何第三方依赖。
3. PowerShell / CMD 进入项目根目录。

## 常用命令

```powershell
# 一次性不中断处理
python -m aggwin.cli run `
  --tables sample/tables.json `
  --queries sample/queries.json `
  --snapshot-dir runtime/snap `
  --result-dir runtime/result `
  --log-file runtime/events.jsonl

# 分批推进（每批 3 行，多次执行同一命令即从快照游标继续）
python -m aggwin.cli run --tables sample/tables.json --queries sample/queries.json `
  --snapshot-dir runtime/snap --result-dir runtime/result --batch-size 3

# 独立验证（参考重算 + 快照交叉校验，固定顺序总结报告）
python -m aggwin.cli verify --tables sample/tables.json --queries sample/queries.json `
  --snapshot-dir runtime/snap --result-dir runtime/result

# 两个结果目录逐字节对照
python -m aggwin.cli compare --left runtime/result_a --right runtime/result_b

# 一键验收（12 个场景，子进程隔离）
python -m aggwin.demo

# 单元测试
python -m unittest discover -s tests -v

# Tkinter 界面
python -m aggwin.cli gui --tables sample/tables.json --queries sample/queries.json `
  --snapshot-dir runtime/snap --result-dir runtime/result
```

## 故障注入

```powershell
# E_BATCH：批次边界落在开窗中途（--strict-batch 时进程退出码 5）
python -m aggwin.cli run ... --batch-size 2 --inject-mid-window --strict-batch

# E_ORDER：把乱序比例阈值压到 0，首个乱序行即报错（退出码 6）
python -m aggwin.cli run ... --ooo-ratio-limit 0

# E_SNAPSHOT：检查点后截断最新快照，下一次恢复报错（退出码 4）
python -m aggwin.cli run ... --inject-corrupt-snapshot
```

## 输出文件

- `result/aggregates.json`：逐分组聚合结果，按 group_key 固定排序；
- `result/windows.json`：逐行窗口发射记录，按 row_id 固定排序；
- `result/trace.json`：每条查询的 canonical 文本与 digest；
- `result/report.json`：游标、批次事件、状态摘要、定点口径与误差上界；
- `result/error.json`：异常时的业务码与定位上下文；
- `snapshots/*.sqlite3`：版本化状态快照，`latest.snapshot` 指向最新一个。
