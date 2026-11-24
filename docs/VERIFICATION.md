# 实际验证结果

## 验证环境

- 实际执行环境：Linux/WSL2，Python `3.10.12`，仅使用标准库。
- 目标交付环境：Windows + Python `3.11`；未使用 3.11 专有 API，`scripts/validate.ps1` 已按 `py -3.11` 编写。
- 当前 Linux 环境未安装 `tkinter` 模块，因此未实际弹出 GUI；已通过 `python -m compileall`，Windows 标准 Python 3.11 自带 Tkinter，可运行 `run_gui.cmd`。
- 完整控制台日志：`docs/verification-run.txt`。
- 真实信号与持久分块篡改恢复日志：`docs/recovery-run.txt`。

## 一键复现

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\validate.ps1
```

或手动执行：

```powershell
py -3.11 -m screen_diag generate --out samples
py -3.11 -m unittest discover -s tests -v
py -3.11 -m screen_diag analyze --samples samples --report reports\baseline.json --state-dir state\baseline
```

## 单元测试

实际结果：

```text
Ran 14 tests in 0.200s

OK
```

覆盖：

- 基线通过与采纳计数；
- 重放规范化摘要逐字节一致；
- 合并冲突定位到分片和规范屏标识；
- 墓碑优先；
- 低内存预算下有界并发加载；
- 摘要损坏、屏数不符、越界时刻、回放偏差；
- `E_SCHEMA` 不写报告/状态目录；
- 中断标记、恢复、持久分块篡改回退；
- 信号处理器置位。

## 基线裁决与合并

命令：

```bash
python3 -m screen_diag analyze --samples samples \
  --report reports/baseline.json --state-dir state/validate-baseline
```

实际结果：

```text
status=PASS exit_code=0
decisions={"adopted": 28, "generation_rollback": 2, "late_dropped": 2, "total": 32}
merge_points=8 conflicts=15 digest_failures=0 fallbacks=0
recovery={"chunk_count": 4, "last_boundary_logic_ms": 8000, "last_chunk_index": 3}
```

15 个冲突均被合并规则成功裁决：12 个来自 v1 陈旧值与 v2 在 4s/6s 检查点的差异，3 个来自 t=8000 健康回退记录与墓碑记录的竞争；墓碑按规则胜出。基线比对使用裁决后的 v2/墓碑胜者，因此整体 `PASS`。

## 异常注入验收

注入清单：`samples/injection_manifest.json`，生成后设置为只读；CLI 的 `--inject` 会复制可变样本到 `out/validation-injected/<case>`，不修改原始样本。

| 注入用例 | 实际退出码/状态 | 实际定位或恢复结果 |
| --- | ---: | --- |
| `corrupt_digest` | 3 / `E_DIGEST` | `shard-001-cp4s` 摘要不匹配；2 个接收屏均回退到 `shard-000-cp2s` |
| `screen_count_mismatch` | 3 / `E_DIGEST` | 同一分片声明屏数比实际多 1；生成 2 条有效回退定位 |
| `time_out_of_bounds` | 2 / `E_SCHEMA` | `sampled_at_ms=999999` 大于 `max_ms=12000`；未生成指定报告或状态目录 |
| `replay_drift` | 4 / `E_REPLAY` | 首个偏差：`logic_ms=3000`、`canon:display:living-room-big`、`field=keyframe_seq`，重放期望 `5`，合并实际 `4` |

回放偏差的真实输出：

```json
{
  "logic_ms": 3000,
  "canonical_screen_id": "canon:display:living-room-big",
  "field": "keyframe_seq",
  "expected_replay_value": 5,
  "actual_merged_value": 4
}
```

## 中断恢复

确定性边界中断：

```bash
python3 -m screen_diag analyze --samples samples \
  --state-dir state/validate-interrupt \
  --report reports/validate-interrupted.json \
  --interrupt-after-chunks 2
```

实际结果：

```text
status=E_INTERRUPTED exit_code=130
recovery={"chunk_count": 2, "last_boundary_logic_ms": 4000, ...}
```

恢复：

```bash
python3 -m screen_diag analyze --samples samples \
  --state-dir state/validate-interrupt \
  --report reports/validate-resumed.json --resume
```

实际结果：

```text
status=PASS exit_code=0
chunk_boundaries=[2000, 4000, 6000, 8000]
```

真实 `SIGTERM` 验证使用 `--chunk-delay-ms 800`，在首个分块处理期间发送信号。进程仍完成当前块并在边界退出：

```text
SIGNAL_EXIT=130
reason=termination_signal:15
recovery={"chunk_count": 1, "last_boundary_logic_ms": 2000}
INTERRUPT_MARKER=yes
```

强制终止后若某个已持久化分块被篡改，恢复只采用摘要有效的连续前缀。实际篡改 `chunk-0002.json` 后：

```text
chunk_count=2
last_chunk_index=1
last_boundary_logic_ms=4000
invalid_chunk.reason=chunk digest mismatch
```

再次 `--resume` 后，失效后缀被重算，结果恢复为 4 个有效分块且 `PASS`。

## GUI 验证建议

在 Windows Python 3.11 上运行：

```powershell
py -3.11 -m screen_diag gui --samples samples
```

建议人工检查：

1. `Screens` 页显示两块屏的代数、关键帧、解码帧、播放位置和分片来源。
2. `Conflicts` 页可按 `logic_ms`、屏标识和字段查看冲突。
3. 双击冲突行打开对应 `shard-*.json`。
4. `Shards` 页显示 5 个有效分片及其 SHA-256 前缀。
5. 点击 `Reveal in file manager` 可在资源管理器中定位 JSON 文件。
