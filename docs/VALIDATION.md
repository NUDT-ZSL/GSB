# 实际验证记录

## 验证环境

本次实际执行位于所提供的 WSL2/Ubuntu 工作区：

- OS：`Linux ... microsoft-standard-WSL2 x86_64`
- Python：`3.10.12`
- NumPy：`1.26.4`
- pytest：`8.2.0`
- SQLite：Python 标准库 `sqlite3`

任务目标环境写的是 Windows 10/11 + Python 3.11/3.12。当前 WSL 中未发现 `powershell.exe`、`pwsh.exe`、`python.exe` 或 `py.exe` 互操作入口，因此没有冒称完成 Windows 实机执行；代码只使用跨平台路径、`sqlite3`、NumPy 和标准库信号/临时文件 API，README/设计文档给出了等价 PowerShell 命令。

## 单元与集成测试

实际命令：

```bash
pytest -q
```

实际结果（完整输出保存在 `reports/pytest-output.txt`）：

```text
.....................                                                     [100%]
22 passed in 24.02s
```

覆盖内容：

- 任务 JSON、阈值、网格、并发和内存字段校验及 JSON Pointer 字段位置。
- 非法任务在创建 `state/`、`output/` 前返回 `E_SCHEMA`。
- 未声明的错误尺寸图像在创建输出前返回 `E_IMAGE`。
- 检测确定性、字典序、真值 IoU 和跨网格线整块保持。
- 并行（并发 1/4）与串行识别的文本、置信度、分数和摘要完全一致。
- 预算轮次纯函数可复现、内存租约释放、异常取消和停止控制器。
- SQLite 防重复主键、同版本续算、版本变化整图重做、块 JSON 篡改重算。
- 第 3 块边界中断、退出码 `130`、中断标记和续算补 8 块。
- 版本化原子输出及旧版本归档。
- 四类异常注入码和其余图像继续处理。
- 信号处理器只设置边界停止事件。
- 固定种子在两个独立目录中生成的 15 个样本/清单文件字节级一致。

## 一键验收脚本

实际命令：

```bash
python scripts/verify_acceptance.py
```

实际结果：退出码 `0`，`reports/acceptance-report.json` 中 `"ok": true`。

关键结果：

- 三张正常表单均检测出 `11/11` 个文字块。
- 重复检测完全相同：`repeatable=true`。
- 最小真值 IoU：`0.785817`、`0.812613`、`0.880832`。
- 平均真值 IoU：`0.889511`、`0.880364`、`0.925351`。
- 三张表单的并行结果与逐块串行结果在文本、置信度和分数上逐项相等。
- 三张表单的真值裁决均为 `adjudication_passed=true`。
- 预算计划重复生成完全一致；合成 12 块在并发 `1/2/4/8` 下的轮次数分别为 `12/6/4/4`。
- 中断场景首次退出码 `130`，写出标记，保留 `3` 个块文件；续算退出码 `1`（四类注入错误的预期部分失败），恢复模式为 `version_match_reuse`，补算 `8` 块后最终 `11` 块且裁决通过。
- 四类异常码分别为：
  - `form-corrupt` → `E_CORRUPT_PIXELS`
  - `form-grid` → `E_GRID_NOT_DIVISIBLE`
  - `form-ledger` → `E_LEDGER_DIGEST`
  - `form-vocab` → `E_VOCAB_MISSING_INJECTED`

## 默认固定样本运行

实际命令：

```bash
python seed_samples.py --root .
python -m formbench run --task samples/task.json
```

实际退出码为 `1`，这是预期的“3 张成功 + 4 张注入业务错误”部分完成状态。标准输出摘要保存在 `reports/default-run.stdout`：

```json
{
  "ok": false,
  "task_id": "game-offline-signup-deterministic",
  "form_count": 3,
  "error_count": 4,
  "results": "output/results.json"
}
```

三张成功表单均为 `block_count=11`、`adjudication.passed=true`。再次运行同一命令时所有块从账本和块存储复用，命令在约 `0.26s` 内完成，仍以退出码 `1` 报告四个注入异常。

## 真实 SIGINT 子进程抽查

另用 `subprocess.Popen` 启动 `python -m formbench run ... --concurrency 4`，约 `0.8s` 后发送真实 `SIGINT`。实际结果：

```text
returncode: 130
INTERRUPTED.json: exists
marker image: form-001
completed_blocks: [0, 1, 2]
```

这说明信号处理器不中途强杀计算，而是在调度轮次/块边界生效，并按约定整数码退出。

## Windows 复验建议

在 Windows PowerShell 中执行：

```powershell
py -3.12 -m pip install numpy pytest
python seed_samples.py --root .
pytest -q
python scripts\verify_acceptance.py
python -m formbench run --task samples\task.json
```

预期 `pytest` 全部通过，验收脚本退出码 `0`；默认 `run` 因内置四类异常注入返回退出码 `1`。
