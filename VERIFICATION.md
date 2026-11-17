# 实际验证记录

验证日期：2026-11-18（Asia/Shanghai）

环境：

```text
Python 3.10.12
pytest 9.2.0
NumPy 2.2.6
```

## 自动化测试

命令：

```bash
.venv/bin/python -m pytest
```

结果：

```text
33 passed
```

覆盖项包括：

- 策略版本、角色引用、令牌引用、重复 ID、结构错误的逐项拒绝。
- 事件会话/令牌格式、事件类型、事件重复、JSON/JSONL 局部失败的继续处理。
- 角色继承与令牌规则引用的传递闭包。
- 环依赖 `E_CYCLE` 与条目序号。
- 更新顺序、事件文件顺序打乱后的受影响集合和传播链一致性。
- 相同/缺失时间戳按版本与稳定键排序。
- 检查点续接、策略版本变化重建、结构版本变化重建、更新水位变化重建。
- 顶层策略版本不变但追加更新时，清空旧事件状态并按新策略重放，避免新旧结论混合。
- 锁冲突 `E_LOCK`。
- 子进程在完整检查点后被强杀，重启只恢复最后完整检查点。
- 半写临时文件忽略与清理。
- 原子替换失败时旧快照保持完整、临时文件清理。
- CLI 成功 `0`、部分失败 `1`、参数错误 `2`。
- 新增显式同步标记不一致检测。

## 主样本：成功路径

命令：

```bash
.venv/bin/python -m authdiag \
  samples/policy.json samples/events_a.json samples/events_b.json \
  --out artifacts/verification/main \
  --workdir artifacts/verification/main
```

退出码：`0`

摘要：

```json
{
  "checkpoint_status": "fresh",
  "status": "success",
  "summary": {
    "active_sessions": 3,
    "active_tokens": 3,
    "affected_entries": 9,
    "affected_sessions": 3,
    "affected_tokens": 3,
    "applied_updates": 3,
    "changed_entries": 3,
    "errors": 0,
    "truth_consistent": 3,
    "truth_inconsistent": 0
  }
}
```

受影响活跃令牌：`tok-alice1`、`tok-alice2`、`tok-dave1`。

受影响活跃会话：`sess-alice1`、`sess-carol1`、`sess-dave1`。

三条同步标记 `sync-001`、`sync-002`、`sync-003` 全部一致。

同一工作目录再次执行时：

```json
{"checkpoint_status": "resumed", "exit_code": 0, "status": "success"}
```

受影响集合与首次执行一致，事件未重复应用。

## 环依赖样本

命令：

```bash
.venv/bin/python -m authdiag \
  samples/cycle_policy.json samples/cycle_events.json \
  --out artifacts/verification/cycle \
  --workdir artifacts/verification/cycle
```

退出码：`1`

诊断：

```json
{
  "code": "E_CYCLE",
  "entry_ids": ["role:a", "role:b"],
  "entry_indices": [0, 1],
  "entry_ordinals": [1, 2]
}
```

虽然存在环，仍保守标记 1 个活跃令牌和 1 个活跃会话，传播链带 `cycle: true`。

## 非法输入与单文件失败

命令：

```bash
.venv/bin/python -m authdiag \
  samples/invalid_policy.json \
  samples/events_malformed.json samples/invalid_events.json \
  --out artifacts/verification/invalid \
  --workdir artifacts/verification/invalid
```

退出码：`1`

实际错误码包括：

```text
E_JSON, E_SESSION_FORMAT, E_TOKEN_FORMAT, E_EVENT_TYPE, E_EVENT_DUP,
E_VERSION, E_ROLE_REF, E_TOKEN_REF, E_DUP, E_SCHEMA
```

坏 JSON 文件被标记为 `failed`，同批另一个事件文件继续处理，并成功建立 1 个活跃会话和 1 个活跃令牌。

## 产出位置

- `artifacts/verification/main/diagnostics.json`
- `artifacts/verification/main/dependency_graph.json`
- `artifacts/verification/main/dependency_graph.dot`
- `artifacts/verification/main/dependency_graph.mmd`
- `artifacts/verification/cycle/diagnostics.json`
- `artifacts/verification/invalid/diagnostics.json`

每条路径下的 `exit_code.txt` / `stdout.json` 记录实际退出码与 CLI 摘要。
