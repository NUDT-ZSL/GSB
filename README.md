# ASR Interop Editor

面向语音识别转写结果的本地领域数据编辑应用。它把 v1 逐词 JSON 与 v2 句级 SRT 统一为 utterance/token 两级 IR，所有时间在内存中以整数毫秒运算，并以版本化原子快照保证编辑序列可恢复。

## 核心能力

- v1 JSON（`version=1`）与 v2 SRT 双解析器，统一保留源格式版本、原始起止时间和文本。
- 非法时间、起止倒置、空文本、非 UTF-8 条目按 `E_FORMAT_INVALID` 隔离，记录文件和行号后继续解析。
- 合并相邻 utterance、按 token 边界拆分、区间整体平移、替换 token 文本；成功后同时重写 v1、v2 与词级旁路元数据。
- v1 → v2 降级保留首 token 起点/末 token 终点，并把词级时间写入 `<stem>.tokens.meta.json`。
- v2 → v1 升级仅在旁路内容哈希匹配时恢复词级时间，否则按字符权重推断时间并输出 `E_TOKEN_TIME_INFERRED`。
- 每次成功编辑后先原子替换完整快照，再原子写输出、状态和进度；启动时按版本与内容指纹回滚。
- SIGINT/SIGTERM/SIGBREAK 会完成当前编辑的快照和进度落盘，以退出码 `0` 退出；中断后按操作序号续跑。
- 输出跨格式 JSON 诊断，含逐句整数毫秒偏差、token 差异、推断标记、快照版本、业务码和舍入统计。

## Windows 快速开始

要求 Python 3.10+。在 PowerShell 中：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m asr_editor generate-samples --out samples
```

所有核心 I/O 使用 `pathlib`、UTF-8 文本和 `os.replace` 原子替换；目录 `fsync` 仅在 POSIX 调用，Windows 路径无需额外配置。

## 命令

```powershell
# 校验音频为 16 kHz、单声道、PCM_16
python -m asr_editor verify-wav --wav samples/sample01.wav

# 初始化工作区快照
python -m asr_editor init `
  --v1 samples/sample01.v1.json `
  --v2 samples/sample01.v2.srt `
  --sidecar samples/sample01.tokens.meta.json `
  --state-dir work/state

# 按计划执行编辑，自动快照并支持断点续跑
python -m asr_editor edit `
  --v1 work/sample01.v1.json `
  --v2 work/sample01.v2.srt `
  --ops operations.json `
  --state-dir work/state

# 显式触发快照恢复
python -m asr_editor recover --v1 work/sample01.v1.json --v2 work/sample01.v2.srt --state-dir work/state

# 输出跨格式诊断
python -m asr_editor diagnose `
  --v1 work/sample01.v1.json `
  --v2 work/sample01.v2.srt `
  --state-dir work/state `
  --out work/diagnostics.json

# 独立格式转换
python -m asr_editor downgrade --input samples/sample01.v1.json --out out/sample01.v2.srt
python -m asr_editor upgrade --input out/sample01.v2.srt --out out/sample01.v1.json --sidecar samples/sample01.tokens.meta.json
```

操作计划是 JSON 数组（也接受 `{"operations": [...]}`）：

```json
[
  {"op": "merge", "utterance_id": "utt-001", "other_utterance_id": "utt-002"},
  {"op": "split", "utterance_id": "utt-001", "token_index": 3},
  {"op": "shift", "start_ms": 1500, "end_ms": 2900, "delta_ms": 250},
  {"op": "replace", "utterance_id": "utt-003", "token_index": 1, "text": "总结"}
]
```

## 运行测试与验收

```powershell
python -m pytest -q
python scripts/run_acceptance.py
```

本机实际结果：`30 passed`；验收脚本 `22/22` 项通过。证据位于 `artifacts/acceptance/`，包括编辑后的 v1/v2、快照、SIGINT 续跑、强杀恢复、失败清单及两份诊断报告。完整结果见 `docs/ACCEPTANCE.md`，设计说明见 `docs/DESIGN.md`。

## 业务码

- `E_FORMAT_INVALID`：解析格式、空文本、起止倒置或编码非法。
- `E_TIME_OVERFLOW`：时间平移会产生负时间戳。
- `E_TOKEN_TIME_INFERRED`：v2 升级时使用了推断词级时间。
- `E_CROSS_FORMAT_MISMATCH`：句级时间偏差超过 20 ms、token 数不同或文本不一致。

失败清单非空时命令行退出码为 `1`；无失败为 `0`。可处理信号的安全退出固定为 `0`。强制终止无法生成中途报告，重启只依据已完整原子替换的快照恢复。
