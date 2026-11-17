# authdiag：离线身份策略增量失效诊断工作台

`authdiag` 是一个纯本地、确定性的身份认证与会话隔离诊断工具。它读取策略声明和会话事件流，在策略增量更新后沿角色继承与令牌模板引用计算传递失效闭包，输出可复现的 JSON 诊断报告、依赖图数据和人工可读图。

运行时代码只使用 Python 标准库，不访问网络、真实账号或外部服务。开发测试使用本地 `.venv` 中的 pytest 与 NumPy。

## 快速开始

```bash
python3 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt

.venv/bin/python -m authdiag.samplegen --out samples
.venv/bin/python -m authdiag \
  samples/policy.json samples/events_a.json samples/events_b.json \
  --out out --workdir out
.venv/bin/python -m pytest
```

如果系统 `venv` 自带 `pip`，也可以用：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

Windows 下将解释器路径替换为 `.venv\Scripts\python.exe`。

## 输入协议

### 策略文件

```json
{
  "schema_version": "1.0",
  "policy_version": "1.0.0",
  "entries": [
    {"id": "session:base", "kind": "session_policy", "version": "1.0.0", "ttl_seconds": 3600},
    {"id": "role:member", "kind": "role", "version": "1.0.0", "parents": []},
    {"id": "role:admin", "kind": "role", "version": "1.0.0", "parents": ["role:member"]},
    {
      "id": "token:admin",
      "kind": "token_rule",
      "version": "1.0.0",
      "refs": ["role:admin", "session:base"],
      "template": "admin-v1"
    },
    {
      "id": "binding:alice",
      "kind": "role_binding",
      "version": "1.0.0",
      "subject": "alice",
      "role": "role:admin",
      "token_rule": "token:admin",
      "session_policy": "session:base"
    }
  ],
  "updates": [
    {
      "update_id": "upd-001",
      "timestamp": "2026-01-01T00:00:00Z",
      "version": "1.1.0",
      "op": "put",
      "entry": {
        "id": "role:member",
        "kind": "role",
        "version": "1.1.0",
        "parents": []
      }
    }
  ],
  "sync_markers": [
    {
      "marker_id": "sync-001",
      "update_id": "upd-001",
      "before_version": "1.0.0",
      "after_version": "1.1.0",
      "changed_entry_ids": ["role:member"],
      "expected_tokens": ["tok-alice1"],
      "expected_sessions": ["sess-alice1"]
    }
  ]
}
```

- 条目 `version`、更新 `version`、顶层 `policy_version` 必须是 `X.Y.Z` 语义版本。
- 支持四种条目：`session_policy`、`role`、`token_rule`、`role_binding`。
- 更新 `op` 支持 `put` 和 `delete`。`delete` 使用 `entry_id` 指向待删除条目。
- `timestamp` 可缺省；所有时间统一规范化为 UTC。
- 同步标记的 `expected_tokens` / `expected_sessions` 是样本内显式观测值。对比只使用标记字段、最终策略依赖图与事件状态，不使用生成器中间传播结果。

### 事件流

单份事件流可以是 JSON 数组或 JSONL。事件字段：

| 类型 | 必需字段 |
| --- | --- |
| `login` | `event_id`, `session_id`, `token_id`, `binding_id` |
| `renew` | `event_id`, `session_id`, `token_id`, `binding_id` |
| `role_change` | `event_id`, `session_id`, `binding_id` |
| `logout` | `event_id`, `session_id` |
| `token_revoke` | `event_id`, `session_id`, `token_id` |

`timestamp` 可选。会话 ID 格式为 `sess-...`，令牌 ID 格式为 `tok-...`，事件 ID 格式为 `evt-...`。非法事件产生带 `source`、`index`、`ordinal`、`offset` 和业务错误码的拒绝记录，后续事件继续处理。

## 传播语义

依赖边方向为“依赖者 -> 被依赖条目”：

- `role_inheritance`：子角色依赖父角色。
- `token_rule_ref`：令牌规则依赖其引用的策略条目。
- `binding_*_ref`：角色绑定依赖角色、令牌规则和会话策略。

被修改条目沿反向边计算可达集合，因此父角色变化会传递到子角色、令牌规则、绑定、令牌和会话。传播使用稳定 BFS 路径；报告中的链按根、节点序列和边种类确定性排序。

环检测使用 Tarjan 强连通分量。发现环时：

- 报告 `E_CYCLE`，并给出 `entry_ids`、零基 `entry_indices` 和一基 `entry_ordinals`。
- 仍按已访问集合保守计算传播闭包，相关链标记 `cycle: true`。
- 退出码为部分失败 `1`。

### 顺序与确定性

增量更新排序键：

1. 有 `timestamp` 的更新在前，缺失者在后。
2. 时间戳升序。
3. 语义版本升序。
4. `update_id` 字典序。

事件排序键为“时间戳存在性、时间戳、`event_id`、来源、序号”。因此更新数组或事件文件传入顺序变化时，最终受影响令牌集合、会话集合和传播链保持一致。字典与集合输出前均排序。

## 检查点、锁与原子写入

- 工作目录为 `--workdir`，检查点文件为 `checkpoint.json`。
- 检查点包含结构版本、顶层策略版本、基础策略身份、已应用更新水位（稳定键 + 内容 SHA-256）、事件水位和最终事件状态。
- 读取时先验证格式、结构版本、策略版本、策略身份和更新前缀；任一不兼容即整体重建，不复用旧失效结论。
- 若在已有事件状态后追加策略更新，会清空事件状态并按新策略重放全部输入事件，避免旧版本结论混入。
- 检查点和四类诊断快照均先写同目录临时文件，`fsync` 后回读校验，再使用 `os.replace` 整体替换。
- 工作目录使用非阻塞 OS 文件锁：Unix 为 `flock`，Windows 为 `msvcrt.locking`。冲突返回 `E_LOCK`。
- 进程被强杀后，检查点只会是旧的完整版本或新写完整的版本；下次获取锁后清理残留 `*.tmp`，再从最后完整检查点续接。

## 输出

输出目录默认包含：

| 文件 | 内容 |
| --- | --- |
| `diagnostics.json` | 可复现诊断报告：错误码/序号/偏移、受影响令牌与会话、更新级与累计传播链、真值对照、文件状态、检查点状态 |
| `dependency_graph.json` | 图数据：节点、依赖边、环、修改标记与受影响标记 |
| `dependency_graph.dot` | Graphviz 人工可读依赖图 |
| `dependency_graph.mmd` | Mermaid 人工可读依赖图 |

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部策略/事件处理成功，无业务错误 |
| `1` | 部分失败；仍生成其余文件和诊断 |
| `2` | 命令行参数错误 |

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `E_SCHEMA` | 对象结构或字段类型非法 |
| `E_VERSION` | 版本不是 `X.Y.Z` |
| `E_ID_FORMAT` / `E_UPDATE_ID_FORMAT` | 策略/更新 ID 格式非法 |
| `E_UNKNOWN_KIND` | 未知策略条目类型 |
| `E_DUP` / `E_UPDATE_DUP` / `E_EVENT_DUP` | 重复 ID；所有副本均拒绝 |
| `E_ROLE_REF` | 角色父引用不存在或类型错误 |
| `E_TOKEN_REF` | 令牌规则引用不存在 |
| `E_BINDING_REF` | 绑定引用不存在或类型错误 |
| `E_SESSION_FORMAT` / `E_TOKEN_FORMAT` | 会话/令牌 ID 格式非法 |
| `E_EVENT_TYPE` / `E_EVENT_REF` / `E_EVENT_STATE` | 事件类型、引用或状态非法 |
| `E_TIMESTAMP` | 时间戳非法 |
| `E_JSON` / `E_IO` | JSON 或文件读取失败 |
| `E_SYNC` | 同步标记与图计算结果不一致 |
| `E_CYCLE` | 策略依赖环，附条目序号 |
| `E_LOCK` | 工作目录被其他写入者锁定 |
| `E_CHECKPOINT_*` | 检查点结构、版本、身份、水位或完整性重建原因 |

## 项目结构

```text
authdiag/
  parser.py      策略/事件逐项规范化与拒绝记录
  graph.py       依赖图、Tarjan 环检测、稳定传播路径
  events.py      会话/令牌状态机
  checkpoint.py  OS 文件锁、原子检查点、兼容性判定
  engine.py      增量应用、恢复、传播映射、真值对照
  report.py      报告图渲染与原子快照
  cli.py         0/1/2 退出码 CLI
  samplegen.py   确定性本地样本生成器
tests/           33 项 pytest 测试
samples/         已生成样本
artifacts/verification/  实际验收运行报告
```

## 本地验证

已在本工作区执行：

```text
Python 3.10.12
pytest 9.2.0
NumPy 2.2.6
.venv/bin/python -m pytest
33 passed
```

样本验收结果详见 `VERIFICATION.md`。实际报告和依赖图保存在 `artifacts/verification/`。

## 建模边界

- 这是离线诊断工具，不执行真实认证，不连接 IdP、数据库或外部服务。
- `token_revoke` 只撤销指定令牌；`logout` 使会话及其令牌失活；`renew` 在模型中签发新令牌并保留旧令牌状态。需要“续期替换旧令牌”的策略可在事件层扩展。
- 失效映射针对当前仍活跃的已签发令牌和活跃会话；已登出/已撤销对象保留在状态审计中，但不计入新的“受影响活跃集合”。
- 强杀本身无法由进程执行清理；工具保证下次加锁恢复时忽略并清理未提交临时文件。
