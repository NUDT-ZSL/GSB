# 实际验证记录

验证日期：2026-11-25。

## 环境

- 主验证解释器：CPython 3.11.18（由 `uv python install 3.11` 安装）。
- 3.11 验证环境 NumPy：2.4.6。
- 兼容性复验：CPython 3.10.12、NumPy 1.26.4。
- 不依赖真实外设、驱动、音频库、Docker 或网络服务。
- 当前执行容器为 WSL/Ubuntu；代码只使用跨平台 `pathlib`、`os.replace`、
  同目录临时文件和 Python 信号 API，路径分隔与保留设备名做了 Windows 安全处理。

## 自动化测试

命令：

```text
uv run --python 3.11 --with 'numpy>=1.24' --with 'pytest>=8.0' python -m pytest -q
```

结果：

```text
................................                                 [100%]
34 passed in 6.27s
```

同一测试集在 CPython 3.10.12 / NumPy 1.26.4 下：

```text
................................                                 [100%]
34 passed in 5.21s
```

静态检查：

```text
uvx ruff check haptic_engine tests generate_samples.py run_pipeline.py verify_results.py
All checks passed!
```

覆盖项：

- 所有要求中的输入校验与 `E_xxx` JSON 错误；非法输入不产生导出流。
- 采样区间换算、分段线性插值、整数量化、重叠事件最大包络。
- 帧内同强度游程合并、每帧指令上限、溢出续帧和逐采样点完整重建。
- 串行与 4 线程并行、预算 3 的指令流逐字节一致；预算峰值不越界。
- 优雅中断退出码 130、块边界中断标记、恢复后与一次跑完逐字节一致。
- SIGTERM 中断退出码 143 和中断标记内容。
- 模拟无中断标记的强制终止残留状态，恢复结果仍与一次跑完一致。
- 块摘要损坏、缺失块的回退补算与 `E_DIGEST` 诊断。
- 输入版本变化时旧状态/旧导出归档并全量重算。
- 版本文件元数据被篡改时识别为无效版本并归档重算。
- 锚点单点毫秒越限返回 `E_SYNC`，不写最终导出。
- 预算等待队列按设备标识排序。

## 固定种子端到端样例

命令：

```text
uv run --python 3.11 --with 'numpy>=1.24' python generate_samples.py \
  --workspace <temp-workspace> --seed 20260214 --force
uv run --python 3.11 --with 'numpy>=1.24' python run_pipeline.py \
  --workspace <temp-workspace> --compact
uv run --python 3.11 --with 'numpy>=1.24' python verify_results.py \
  --workspace <temp-workspace>
```

运行结果：

```json
{
  "business_codes": [],
  "exit_code": 0,
  "ok": true,
  "scheduling": {
    "executor_workers": 3,
    "in_flight": 0,
    "limit": 4,
    "peak": 4,
    "wait_events": 48,
    "wait_seconds": 0.201497641
  },
  "status": "completed",
  "totals": {
    "commands": 214,
    "devices": 4,
    "frame_blocks": 114,
    "splits": 60
  }
}
```

逐设备结果：

| 设备 | 采样点 | 帧块 | 指令 | 拆分 |
| --- | ---: | ---: | ---: | ---: |
| `dev_glove_left` | 120 | 34 | 61 | 22 |
| `dev_glove_right` | 96 | 25 | 42 | 13 |
| `dev_vest_core` | 60 | 14 | 24 | 8 |
| `dev_wrist_band` | 236 | 41 | 87 | 17 |

锚点共 12 组设备-锚点诊断，最大 `abs(delta_ms) = 0.0`。逐采样点重建验证通过，
导出的 214 条指令与内存中确定性规划逐条相同。

独立复核：

```json
{
  "business_codes": [],
  "ok": true,
  "totals": {
    "anchors": 12,
    "commands": 214,
    "devices": 4,
    "frame_blocks": 114
  }
}
```

## 人工检查点

- 块数与完成范围连续，续帧仍引用原始 `source_frame_index`。
- 每块指令数不超过设备上限；样例中大量交替包络触发 60 次续帧拆分。
- 状态块先写同目录临时文件再替换；源码中不存在直接覆盖块/版本/导出的路径。
- 最终 `export/commands.json` 在串行、并行、中断恢复和摘要修复场景中逐字节一致
  （测试直接比较文件字节）。
