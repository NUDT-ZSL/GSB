# 本地对象存储分片上传会话管理器

这是一个不依赖外部服务的 Python 3.10+ 领域组件与 CLI。它把一次对象上传建模为多个并发上传会话，并在可注入逻辑时钟、SQLite 事务和本地文件锁下完成分片覆盖、版本演进、整体摘要仲裁、单完成者裁决、崩溃恢复与孤儿分片回收。

## 快速开始

Windows PowerShell：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m pytest
```

Linux / macOS：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest
```

如不安装包，也可在仓库根目录直接执行 `python -m multipart_store ...`。运行目录默认为 `./.multipart-store`，也可用 `--data-dir` 或环境变量 `MULTIPART_STORE_DIR` 指定。

## 样本

- `samples/primary.json`：覆盖上传、同内容重复上传、同对象双会话竞争、分片缺失、校验和不符、整体摘要不符。
- `samples/budget.json`：每轮 150ms 预算与 50ms 步进，五个分片分两轮启动。
- `samples/clock_rollback.json`：同会话逻辑时钟从 1000ms 回拨到 100ms，容差 50ms。
- `samples/competing.json`：两个内容完全合法的会话并发完成同一对象，用于验证单完成者。

主样本的最终对象为 `alpha-overwritten\nbravo\ncharlie\n`，其 SHA-256 为：

```text
f380dfc970bac60244e3f606546aca26db8d35582f8fcec49463b56af157c9af
```

## 命令行流程

```bash
WORK=$(mktemp -d)

python -m multipart_store --data-dir "$WORK" load samples/primary.json
python -m multipart_store --data-dir "$WORK" upload samples/primary.json
python -m multipart_store --data-dir "$WORK" complete --session-id sess-main
python -m multipart_store --data-dir "$WORK" status
python -m multipart_store --data-dir "$WORK" verify --session-id sess-main
```

`complete --session-id sess-main` 会中止同对象竞争会话 `sess-shadow`，并记录：

```text
aborted_trigger = "session:sess-main"
aborted_reason  = "another session completed the object"
```

三个失败会话可分别触发不同业务码：

```bash
python -m multipart_store --data-dir "$WORK" complete --session-id sess-missing
# E_PART_MISSING

python -m multipart_store --data-dir "$WORK" complete --session-id sess-checksum
# E_CHECKSUM_MISMATCH

python -m multipart_store --data-dir "$WORK" complete --session-id sess-digest
# E_DIGEST_MISMATCH
```

完成判定之后迟到的分片会被写入 `late_uploads`，但不改变会话的 `visible_version` 与已完成裁决：

```bash
python -m multipart_store --data-dir "$WORK" upload \
  --session-id sess-main --part-number 0 \
  --content-text late --checksum sha256:$(printf late | sha256sum | cut -d' ' -f1) \
  --logical-time-ms 3000 --event-id 990
```

预算样本第一轮只启动 3 个事件，事件 4、5 记录为 `abandoned_budget` 且保持 pending；第二轮完成剩余两个：

```bash
BUDGET=$(mktemp -d)
python -m multipart_store --data-dir "$BUDGET" load samples/budget.json
python -m multipart_store --data-dir "$BUDGET" upload
python -m multipart_store --data-dir "$BUDGET" upload
python -m multipart_store --data-dir "$BUDGET" complete --session-id sess-budget
```

逻辑时钟样本中第一个分片成功，第二个分片返回 `E_CLOCK_REGRESSION`，不会进入已提交可见集合：

```bash
CLOCK=$(mktemp -d)
python -m multipart_store --data-dir "$CLOCK" load samples/clock_rollback.json
python -m multipart_store --data-dir "$CLOCK" upload
```

## 强制终止与恢复

组件内置确定性故障点，仅用于本地验证。故障点通过进程被强制退出模拟，恢复必须依据 SQLite 与磁盘事实，而不是内存：

```bash
CRASH=$(mktemp -d)
python -m multipart_store --data-dir "$CRASH" load samples/budget.json
python -m multipart_store --data-dir "$CRASH" upload
python -m multipart_store --data-dir "$CRASH" upload

MULTIPART_FAULT_POINT=completion_after_prepare \
  python -m multipart_store --data-dir "$CRASH" complete --session-id sess-budget
# 进程退出码 75，completion_attempts.status = prepared

python -m multipart_store --data-dir "$CRASH" resume --cleanup
python -m multipart_store --data-dir "$CRASH" verify --session-id sess-budget
```

可用故障点：

- `part_after_rename`：分片已原子改名，但分片与事件元数据尚未提交。
- `completion_after_prepare`：完成意图已持久化，尚未校验与拼接。
- `completion_after_validation`：摘要校验完成，最终文件尚未物化。
- `completion_after_materialize`：最终文件已原子落盘，最终裁决尚未提交。
- `completion_after_commit`：最终裁决已提交，可用于验证幂等响应。

`MULTIPART_FAULT_EXIT` 可覆盖退出码，默认 75。

## 命令和输出

```text
load MANIFEST
upload [MANIFEST] [--round N]
upload --session-id ID --part-number N --content-* ... --checksum ALGO:HEX --logical-time-ms MS
complete SESSION_ID
abort SESSION_ID --reason TEXT
resume [--cleanup]
status
verify [--session-id ID]
```

所有命令输出 UTF-8 JSON。`status` 使用 `sort_keys=True`，会话、事件、对象、孤儿清单都按稳定键排序。错误形如：

```json
{
  "error": {
    "code": "E_DIGEST_MISMATCH",
    "details": {
      "actual_digest": "...",
      "expected_digest": "...",
      "first_bad_part": 0
    },
    "message": "concatenated object digest does not match declaration"
  },
  "ok": false
}
```

## 集成 API

```python
from multipart_store import MultipartStore, load_manifest

store = MultipartStore("./runtime")
manifest = load_manifest("samples/primary.json")
store.load(manifest)
store.upload_round()
store.complete("sess-main")
assert store.verify("sess-main")["valid"]
```

完整示例见 `examples/integration.py`，技术细节见 `docs/DESIGN.md`。

## 实际验证

在 Python 3.10.12、pytest 8.3.3 下执行：

```text
python -m pytest -q
........................................                                 [100%]
40 passed
```

覆盖内容包括：非法清单不落盘、覆盖与幂等文件复用、逻辑时钟回拨、三类完成失败、预算放弃、迟到分片、分片/完成阶段强杀恢复、孤儿回收、多进程同事件幂等、多进程单完成者裁决、稳定 status 与 verify。

更完整的逐项输出见 `docs/VALIDATION.md`。当前实测环境为 Linux/WSL2；Windows 锁与路径分支已实现，但未在原生 Windows 主机上复测。
