# 实际验证记录

验证日期：2026-11-25（Asia/Shanghai）。

## 验证环境

- 实际执行环境：Linux 容器，Python `3.10.12`。
- `numpy==1.26.4`。
- 已安装 PyQt5 发行包 `5.15.11` / Qt `5.15.14`，使用 `QT_QPA_PLATFORM=offscreen` 完成无显示器界面冒烟。
- 交付依赖固定为 `numpy==1.26.4`、`PyQt5==5.15.10`；代码使用 Python 3.10/3.11 兼容语法和 `pathlib`，未使用 Docker 或外部数据集。

Windows Python 3.11 目标安装命令：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install numpy==1.26.4 PyQt5==5.15.10
```

本地 wheel 离线安装：

```powershell
python -m pip install --no-index --find-links .\wheels numpy==1.26.4 PyQt5==5.15.10
```

## 自动化测试

执行：

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 15 tests in 2.423s

OK
```

覆盖：

- 多种形状、分块、并行度、生长半径、形态学核下，分块结果与整幅串行结果逐体素相同。
- 调度轮次、内存准入、逻辑时间准入和注入时钟可复现。
- `E_DIMENSION`、`E_SPACING`、`E_CHUNK`、`E_THRESHOLD`、`E_MORPHOLOGY`、`E_PARALLELISM`、`E_MEMORY_BUDGET`、`E_JSON`、`E_VOLUME`。
- raw 修订与 WYSIWYG final 掩码修订的精确失效、增量一致性和二次运行复用。
- `E_DIGEST` 损坏识别与自动补算。
- 版本变化归档旧版本。
- 可处理中断后的恢复。
- 残留 `.tmp` 与缺失块恢复。
- 导出版本相同跳过、修订后替换。
- PyQt offscreen 启动、绘制、选择、拖拽修订、评估和导出路径。

完整日志保存在 `artifacts/unittest-final.log`。

## 一键验收

执行：

```bash
python -m medseg_workbench.acceptance --out artifacts/acceptance
```

实际摘要（完整 JSON：`artifacts/acceptance/acceptance-result.json`）：

```json
{
  "ok": true,
  "initial": {
    "exact_vs_full_serial": true,
    "chunks": 27,
    "rounds": 7
  },
  "revision": {
    "stale_chunks": ["000_001_001", "001_001_001"],
    "recomputed": ["000_001_001", "001_001_001"],
    "reused_count": 25
  },
  "incremental": {
    "exact_vs_full_with_same_revisions": true,
    "second_run_completed": [],
    "second_run_reused": 27
  },
  "final_mask_revision": {
    "stale_chunks": ["000_001_001", "001_001_001"],
    "recomputed": ["000_001_001", "001_001_001"],
    "exact_vs_full_with_same_edits": true
  },
  "recovery": {
    "interrupt_exit_code": 130,
    "interrupt_marker_written": true,
    "exact_vs_full_serial": true
  },
  "evaluation": {
    "dice": 0.9098599492433499,
    "iou": 0.8346266597689257,
    "precision": 0.9901800327332242,
    "recall": 0.8415927664753956,
    "invalid_count": 0
  },
  "seams": {"seam_count": 54}
}
```

## 可处理终止信号

在确定性样本上加入 `--chunk-delay-seconds 0.5`，运行中发送 `SIGINT`：

```bash
python -m medseg_workbench run --task artifacts/interrupt/sample/task.json \
  --chunk-delay-seconds 0.5 &
kill -INT <pid>
```

实际结果：

- Shell 观测退出码：`130`。
- 写入 `state/INTERRUPTED.json`。
- 已完成块均有有效 `.npz`；未完成块没有可误认作完成的 `.npz`。
- 重启后复用 8 个已完成块、补算 19 个块。
- 拼接掩码与全量串行参考逐体素一致：`diff = 0`。

## 强制终止恢复

使用 `kill -9` 模拟强制终止：

```bash
python -m medseg_workbench run --task artifacts/kill/sample/task.json \
  --chunk-delay-seconds 0.6 &
sleep 1
kill -9 <pid>
```

实际终止后状态：

```text
kill_wait_code=137
persisted_chunks=4
temp_files=0
interrupt_marker=no
```

重启结果：

```text
recovered_reused 4
completed 23
kill recovery exact: True
```

这符合“强制终止后不承诺退出报告，只依据已持久化且摘要有效的分块恢复”的边界。

## 真值隔离检查

分割、调度、失效判定模块不打开 `truth/` 目录；只有 `medseg_workbench/evaluation.py` 调用 `read_stack(..., truth=True)`。真值仅用于 Dice/IoU、精确率、召回率等报告统计。
