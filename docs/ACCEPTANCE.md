# 验收记录

## 环境

实际执行环境：

- Python `3.11.0rc1`
- Linux `6.18.33.2-microsoft-standard-WSL2` x86_64（Windows 主机上的 WSL2）
- numpy `2.4.6`
- soundfile `0.14.0`
- pytest `9.2.0`

代码不使用容器。核心 I/O 与信号逻辑按 Windows 兼容方式实现；POSIX 专属的目录 `fsync` 已由 `os.name == "posix"` 隔离。Windows 运行命令见根目录 `README.md`。

## 复现命令

```bash
python -m pytest -q
python scripts/run_acceptance.py
```

实际测试结果：`30 passed`。

验收脚本结果：`status=passed`，`22/22` 项检查通过。完整机器可读结果在 `artifacts/acceptance/acceptance-summary.json`。

## 样本与音频

`samples/` 由 `asr_editor.samples.generate_samples`（或 `scripts/generate_samples.py`）确定性生成：

- `sample01`：v1/v2 一致，用于四类编辑与升级恢复。
- `sample02`：v2 首句偏移 30 ms，第二句 token 切分不一致，用于 `E_CROSS_FORMAT_MISMATCH`。
- `sample03`：v1 含 0.4505 秒等亚毫秒输入，用于舍入统计。

三个 WAV 均验证为 `16000 Hz / 1 channel / PCM_16`，两次生成字节一致。清单和 SHA-256 见 `samples/manifest.json`。

## 四类编辑与输出等价

实际执行序列：

1. 合并 `utt-001` 与 `utt-002`。
2. 在新 `utt-001` 的第 3 个 token 边界拆分。
3. 将 `[1500,2900)` ms 内 token 整体平移 `+250` ms。
4. 将 `utt-003` 的 token `报告` 替换为 `总结`。

成功快照版本依次为 `1, 2, 3, 4`。拆分严格保留原词级时间：第一句结束仍为 `1.250 s`，第二句起点为 `1.500 s`，没有把前一词拉伸到后句起点。最终 v1/v2 诊断结果：

- utterance 数：v1 `3`，v2 `3`
- mismatch 数：`0`
- token 数：两侧一致
- 文本：逐句一致
- 推断时间标记：`9`（来自独立解析 v2 的句级 token 化）
- 网格舍入：`6` 次，最大 `3.333 ms`
- `meets_10ms_granularity = true`

报告：`artifacts/acceptance/diagnostics-sample01-edited.json`。

## 降级/升级

- 降级写出 v2 与旁路词级元数据；v2 句界取首/末 token。
- 哈希匹配升级：`sidecar_used=true`，`inferred_time_count=0`，词文本与起止毫秒逐 token 与编辑后 IR 一致。
- 无旁路升级：所有 token 标记为推断，`inferred_time_count == token_count`，业务码含 `E_TOKEN_TIME_INFERRED`。
- 篡改旁路后的升级测试：记录 `E_SIDECAR_INVALID` 失败，回退推断而非静默采用坏数据。

## 快照恢复与一致性

- 每个编辑前缀 `1..4` 都在独立工作区执行后提升 `workspace.json` 版本，再重启恢复；恢复后 `Transcript.to_dict()` 与按相同序列连续操作逐字段一致。
- 修改工作区版本为 `+100` 后启动，实际恢复原因包含“工作区版本高于最新完整快照”，并回到版本 `4`。
- 破坏当前 v2 后启动，完整快照可重建合法 v1/v2/旁路文件。
- 模拟“版本化快照已写、latest 尚未替换”时，加载器选择最高完整版本。

## 信号与强杀

- SIGINT 实测：退出码 `0`，`graceful_signal=SIGINT`，在完成第 `1` 个操作后写入快照/进度；同计划重启从操作 `2` 继续，最终完成全部 8 个操作。
- SIGKILL 实测：进程被强制终止后 `recover` 退出码 `0`，只恢复完整快照版本 `1`，失败清单为空；没有中途退出报告。
- SIGINT/SIGKILL 实测来自 WSL2 POSIX 环境；Windows 可处理信号路径使用同一安全落盘代码和标准库信号分支。

## 解析失败隔离

验收夹具同时包含合法项和起止倒置项：

- v1 保留前后两个合法 utterance，坏项位于第 5 行。
- v2 保留前后两个合法 cue，坏项位于第 6 行。
- 失败清单均为 `E_FORMAT_INVALID`。
- `inspect` 命令因失败清单非空返回整数退出码 `1`。

失败证据：`artifacts/acceptance/failure-list.json`。

## 跨格式诊断

`sample02` 实际结果：

- mismatch utterance 数：`2`
- 第一句起始偏差：`30 ms`（超过 20 ms 阈值）
- 第二句 token 数差：`2`（v1 3 个，v2 1 个）
- 业务码含：`E_CROSS_FORMAT_MISMATCH`、`E_TOKEN_TIME_INFERRED`
- 精度：`1` 次网格舍入，最大 `5.000 ms`，满足半网格约束

报告：`artifacts/acceptance/diagnostics-sample02-mismatch.json`。

## 业务码清单

实际验收观测到：

| 业务码 | 触发证据 |
|---|---|
| `E_FORMAT_INVALID` | 非法 v1 token 起止倒置；非法 v2 cue 起止倒置 |
| `E_TIME_OVERFLOW` | 对从 0 ms 开始的时间段执行 `-10 ms` 平移 |
| `E_TOKEN_TIME_INFERRED` | 无旁路升级 v2；独立诊断 v2 token 推断时间 |
| `E_CROSS_FORMAT_MISMATCH` | sample02 的 30 ms 偏差与 token 数差 |

核心退出码：正常命令 `0`；失败清单非空 `1`；可处理信号安全退出 `0`；未捕获的 Ctrl+C 兜底 `130`。

## 产物索引

- `artifacts/acceptance/acceptance-summary.json`：22 项检查、环境、业务码及信号结果。
- `artifacts/acceptance/diagnostics-sample01-edited.json`：编辑后零偏差报告。
- `artifacts/acceptance/diagnostics-sample02-mismatch.json`：故意不一致报告。
- `artifacts/acceptance/failure-list.json`：解析失败清单。
- `artifacts/acceptance/pytest.txt`：`30 passed` 的原始测试输出。
- `artifacts/acceptance/work/sample01/`：编辑后的 v1、v2、旁路文件。
- `artifacts/acceptance/state/`：版本 `0..4` 快照、状态和进度。
- `artifacts/acceptance/signal-state/`、`forced-kill-state/`：信号与强杀恢复状态。
