# 实际验证记录

验证日期：2026-11-25（Asia/Shanghai）。

## 验证环境

- 实际执行环境：WSL2 / Linux（`Linux ... microsoft-standard-WSL2 x86_64`）。
- Python：`3.10.12`。
- NumPy：`1.26.4`。
- 当前 WSL PATH 中没有 `powershell.exe` / `pwsh.exe` / `py.exe`，因此未在此环境真实执行 Windows PowerShell；已提供 `scripts/run_demo.ps1`，实现只使用 `pathlib`、`os.replace`、标准信号和临时文件接口，语法兼容 Python 3.11/3.12。

任务要求 Python 3.11/3.12；代码启用了 `from __future__ import annotations`，未使用 3.11/3.12 独有 API，因此在指定 Windows Python 版本可运行。下列结果是本次在 Python 3.10 环境的真实验证。

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

原始输出保存在 `docs/validation-unittest.txt`。结论：

```text
Ran 19 tests in 4.218s

OK
```

覆盖：

- 合法样本解析、重复节点、未声明状态键、缺失跳转、越界/非有限阈值。
- 非法 CLI 输入返回 `E_SCHEMA` 且不创建状态目录或报告文件。
- 定点/float64 残差溯源、定点阈值通过而 float64 反事实不通过。
- 因果指针、效果日志和终局状态重建一致。
- 同剧本同选择序列的规范化报告摘要一致。
- 22 条全分支路径、11 个死锁分支、1 个节点级全状态死锁。
- 分支枚举顺序反转后的核心结果一致。
- 事件边界中断、续算、摘要损坏回退、事件序号不符、版本变化归档。
- 外部 `SIGTERM` 子进程退出码 `130`。
- 有界线程池并发度、内存预算、结果顺序和分块异常取消。

## 本地确定性样本

生成命令：

```bash
python3 -m narrative_engine.cli generate-sample --out samples --seed 351
```

生成文件：

- `samples/story.json`
- `samples/story_bundle.json`
- `samples/state_schema.json`
- `samples/choice_sequence.json`
- `samples/choice_sequences.json`

样本版本标识：

```text
ver-d9ea1e1622123f32
```

## 定点与 float64 残差

黄金路径命令：

```bash
python3 -m narrative_engine.cli run \
  --script samples/story.json \
  --choices samples/choice_sequence.json \
  --state-dir runtime/state \
  --report runtime/run_report.json
```

实际结果：

- 终局状态：`terminal`。
- 执行事件数：`13`。
- 节点数：`3`。
- 终局 `trust` 定点值：`1050000`（十进制 `1.05`，包含保险库 `+0.2` 与结局 `+0.05`）。
- 首次残差越界：事件序号 `8`，事件 `evt_echo_trust_08`，状态键 `trust`，节点 `node_arrival`。
- 绝对差：`1.1102230246251565e-16`。
- 容差：`1e-16`。
- 阈值事件 `evt_decode_eight_echoes`：定点 `800000 >= 800000` 为 `true`；float64 参考实际值 `0.7999999999999999`，反事实判定为 `false`。
- CLI 内置因果一致性校验通过，未返回 `E_CAUSAL`。

## 分支与死锁

命令：

```bash
python3 -m narrative_engine.cli explore \
  --script samples/story.json \
  --report runtime/branch_report.json
```

实际结果：

```text
path_count=22
deadlock_count=11
deadlock_nodes=['node_locked_archive']
```

`node_locked_archive` 的所有 11 个可达状态均无启用选择；报告给出每个状态的定点整数快照、十进制值和状态摘要。反转选择枚举顺序后，`(choice_ids, node_count, event_count, state_digest, deadlocked)` 序列完全一致。

并发选择序列预计算命令：

```bash
python3 -m narrative_engine.cli explore \
  --script samples/story.json \
  --choice-sequences samples/choice_sequences.json \
  --workers 4 --chunk-size 2 \
  --report runtime/sequence_precompute_report.json
```

实际结果：`sequence_count=7`、`worker_count=4`、`chunk_count=4`。

## 中断、摘要和恢复

确定性事件边界中断：

```bash
python3 -m narrative_engine.cli run ... --state-dir /tmp/narrative-resume --stop-after-events 3
echo $?   # 130
```

实际结果：

- 退出码：`130`。
- `interrupt.json` 记录 `event_seq=3`。
- `snapshots/` 中存在 `snapshot-00000001.json` 至 `snapshot-00000003.json`。
- 再次执行不带 `--stop-after-events` 的同一命令后：`recovery.action=resumed`，最终 `event_count=13`。
- 恢复报告与全新运行报告在世界状态、事件顺序、因果链和首次残差记录上逐字段相等。

摘要损坏回退：

- 篡改 `snapshot.json` 但不更新摘要后，`verify-snapshot` 返回 `E_DIGEST` / 退出码 `3`。
- 重启自动回退到 `snapshots/snapshot-00000005.json` 等最近有效序号快照，并在报告 `warnings` 中记录回退。
- 重算摘要但把 `event_seq` 改为 `99` 时，校验返回 `reason=event_seq_mismatch` 的 `E_DIGEST`。

版本变化：

- 修改剧本节点标题以改变规范化版本后，同一状态目录重启返回 `recovery.reason=version_mismatch`。
- 旧 `snapshot.json` 与 `snapshots/` 被移动到 `state/archive/version_mismatch-.../`，推演从头完成。

强制 `SIGKILL` 演练：

- 使用 `--event-delay-ms 300` 启动子进程，约 `0.9s` 后执行 `process.kill()`（POSIX `SIGKILL`）。
- 实际信号返回码：`-9`。
- 已持久化 `3` 份序号快照。
- 强杀后不存在优雅中断标记 `interrupt.json`。
- 重启仅依据有效快照恢复，返回码 `0`，最终状态 `terminal`、`event_count=13`。

## 复现完整演示

POSIX/WSL：

```bash
./scripts/run_demo.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_demo.ps1
```
