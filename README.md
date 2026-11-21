# 依存弧三格式互操作组件

本工作区提供一个可本地集成的 Python 3.11 组件，用于依存句法分析弧选择结果的格式读取、双向迁移、语义等价裁决、往返一致性校验与配置快照治理。运行时不访问网络，不使用 Docker 或其他容器。

核心约束：

- 同一句弧结果可在 v1、v2、v3 三种表示间转换。
- 迁移绝不改变 `token_id -> head` 指向。
- v1 无法表达的字段只在迁移阶段按版本化策略补全，并逐项记录来源。
- v3 降级到 v1/v2 时，无法表达的双向弧链、跨句引用等按策略丢弃，并输出明细。
- 非默认扩展值不能靠补默认值伪装成“等价”，而是返回 `E_SEMANTIC_DRIFT`。
- 受控集合和迁移策略按版本号 + 内容 SHA-256 整体替换，不做增量修补。
- 资源超限可严格报错，也可降级为逐句流式迁移，且已输出弧图保持不变。

## 快速开始

### Linux/macOS

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python -m dep_arc_interop.cli demo --compact
.venv/bin/python -m unittest discover -s tests -v
```

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install numpy
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\dep-arc-interop.exe demo --compact
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

所有文件读写显式使用 UTF-8，路径使用 `pathlib.Path`，样本与报告采用 `\n` 换行。代码路径不依赖 POSIX shell、符号链接或 Unix 权限位。当前验收容器实际平台为 Linux，Windows 兼容性按上述跨平台 API 设计确认，但本环境未在 Windows 实机执行。NumPy 仅用于固定种子的样本生成，业务组件本身不依赖外部服务。

常用入口：

```bash
dep-arc-interop generate                 # 重新生成确定性样本
dep-arc-interop demo                     # 全量验收并写报告
dep-arc-interop demo --compact           # 仅输出摘要
dep-arc-interop selfcheck                # 跨模块一致性自检
python examples/integration.py           # 最小集成示例
python examples/run_demo.py              # 完整示例
```

## 已实现结果

本次实际验证环境：

- Python `3.11.18`
- NumPy `2.4.6`
- 本地确定性种子：`20261122`
- 样本：5 句、42 个 token
- 自动化测试：`35` 项全部通过
- 异常/边界注入：`20/20` 命中预期业务码
- 完整报告：`artifacts/validation_report.json`

实际结果摘要：

| 校验项 | 结果 |
| --- | --- |
| v1/v2/v3 读取 | 5 句、42 节点，全部成功 |
| 三格式语义等价 | 相同 42 条弧、5 个根、相同 deprel |
| 9 组双向迁移 | 全部成功，审计重放差异均为 0 |
| v1 -> v2 | 补全 89 项：84 个置信度字段 + 5 个根标记 |
| v1 -> v3 | 补全 183 项：置信度、根、正逆向标记及空扩展字段 |
| v2 -> v3 | 补全 94 项：正逆向标记与空链/引用字段 |
| v3 -> v2 | 丢弃 84 项正逆向标记 |
| v3 -> v1 | 丢弃 173 项：根、置信度、正逆向标记 |
| 富 v3 -> v1 | 丢弃 175 项，其中包含双向弧链与跨句引用 |
| v1/v2/v3 往返 | 三种格式均弧级一致 |
| 资源降级 | 在 `S003` 触发；此前已输出 2 句/17 节点；42 条弧与批处理一致 |
| 自检 | `SNAPSHOT_TRACE`、`FORMAT_VIEW_ARCS`、`AUDIT_REPLAY` 全部通过 |

基础 v1/v2/v3 的置信度均取策略默认区间 `[0.0, 1.0]`，正逆向标记均为 `true`。因此从 v1 补全后可与 v2/v3 的默认值等价；但成功结果会返回 173 条 `reconstructed_fields` 审计项，不会静默补值。若 v3 使用非默认置信度、非默认校验标记、双向弧链或跨句引用，则与 v1/v2 裁决时返回 `E_SEMANTIC_DRIFT`。

## 三种格式契约

公共约束：

- `token_id` 从 1 开始且连续。
- `head = 0` 表示虚拟根。
- 每句恰好一个根弧。
- `deprel` 必须属于当前受控依存关系集合。
- 弧图必须无环。

### v1：无字段名竖线行

数据行没有字段名；文件级 `# format=v1` 仅用于分派，也可通过三字段行自动识别。

```text
# format=v1
# sent_id=S001
1|3|det
2|3|amod
3|4|nsubj
4|0|root
```

v1 不包含显式根标记、置信区间、正逆向校验标记、双向弧链和跨句引用。根由唯一 `head=0` 弧推断。

### v2：命名字段行 + 显式根

```text
# format=v2
# sent_id=S001
# root=4
token_id=1|head=3|deprel=det|confidence_low=0.000000|confidence_high=1.000000
token_id=3|head=4|deprel=nsubj|confidence_low=0.000000|confidence_high=1.000000
token_id=4|head=0|deprel=root|confidence_low=0.000000|confidence_high=1.000000
```

每句必须有且只有一个 `# root=<token_id>`。根标记必须与唯一 `head=0` 弧一致。

### v3：结构化嵌套 JSON

```json
{
  "format": "deparc",
  "version": "v3",
  "sentences": [
    {
      "sent_id": "S001",
      "root": 4,
      "tokens": [
        {
          "token_id": 4,
          "head": 0,
          "deprel": "root",
          "confidence": {"low": 0.0, "high": 1.0},
          "dependency_checks": {"forward": true, "reverse": true}
        }
      ],
      "bidirectional_arc_chains": [
        {
          "chain_id": "S001:bc:1",
          "token_ids": [3, 4, 5],
          "forward": true,
          "reverse": true
        }
      ],
      "cross_sentence_refs": [
        {
          "source_token_id": 8,
          "target_sent_id": "S002",
          "target_token_id": 3,
          "relation": "bridging",
          "confidence": 0.87
        }
      ]
    }
  ]
}
```

`bidirectional_arc_chains` 与 `cross_sentence_refs` 是 v3 必需字段；无内容时写空数组。跨句引用目标句和 token 必须存在于同一 v3 文档。每个弧必须含置信区间和 `forward/reverse` 校验标记。

## 内部模型与迁移规则

统一内部模型为 `ArcGraph -> SentenceGraph -> Arc`：

- `Arc` 保存 `token_id`、`head`、`deprel`、可选置信区间、可选正逆向标记与字段来源。
- `SentenceGraph` 保存显式根标记、根来源、双向弧链、跨句引用。
- v1 未观测字段保持 `None`，只在迁移时按策略补全，解析阶段不造值。
- 每次迁移产出 `fills` 与 `drops`，每项包含 `sent_id/token_id/field/value/source/reason/policy_version/policy_checksum`。
- `MigrationResult.replay()` 使用源图和审计记录重建目标图；自检会对 9 个格式方向逐一比较。

迁移矩阵：

| 源 -> 目标 | 行为 |
| --- | --- |
| v1 -> v2 | 从唯一 `head=0` 推断根；补默认置信区间 |
| v1 -> v3 | 推断根；补默认置信区间、正逆向标记、空链/引用 |
| v2 -> v1 | 丢弃根标记和置信区间，根重新由弧推断 |
| v2 -> v3 | 保留根与置信区间；补正逆向标记、空链/引用 |
| v3 -> v2 | 保留根与置信区间；丢弃正逆向标记、双向链、跨句引用 |
| v3 -> v1 | 丢弃根标记、置信区间、正逆向标记、双向链、跨句引用 |

所有丢弃字段必须出现在当前迁移策略的 `discard_rules.downgrade_to_v1` 或 `discard_rules.downgrade_to_v2` 中；否则返回 `E_INCONSISTENT`。

## 语义校验

校验顺序与错误码：

| 条件 | 业务码 | 定位 |
| --- | --- | --- |
| `deprel` 不在当前受控集合 | `E_DEPREL_INVALID` | `sent_id`、`token_id`、`field=deprel` |
| `head` 不是 0 或句内 token | `E_HEAD_RANGE` | `sent_id`、`token_id`、`head` |
| 根数量不为 1、显式根与根弧冲突 | `E_ROOT_VIOLATION` | `sent_id`、`token_id`、`field=root` 或 `head` |
| 依存弧成环 | `E_CYCLE_DETECTED` | `sent_id`、`token_id`、`cycle` |
| token id 非连续 | `E_TOKEN_SEQ` | `sent_id`、`token_id`、`expected` |
| 重复 `sent_id` | `E_DUP_SENT` | `sent_id`、行号 |
| POS 不在当前受控集合 | `E_POS_INVALID` | `sent_id`、`token_id`、`field=pos` |

## 等价裁决

`judge_equivalence` 要求：

1. 三种视图的句子集合、token 集合一致。
2. 相同 token 的 `head`、`deprel` 一致。
3. 有效根 token 一致。
4. v2/v3 置信区间一致；v3 正逆向标记一致。
5. v1/v2 缺失、而 v3 存在的扩展字段，仅当值等于策略默认值时可重建，并写入 `reconstructed_fields`。
6. 双向弧链、跨句引用或非默认扩展值无法被 v1/v2 表达时，返回 `E_SEMANTIC_DRIFT`，列出 `unrestorable_fields`、`tokens` 与逐项 `drift`。

## 往返一致性

`verify_roundtrip` 对同一格式执行 `读取 -> 序列化 -> 再读取 -> 比较`：

- 所有格式逐 token 校验 `token_id`、`head`、`deprel`。
- v2/v3 在策略精度内校验置信区间。
- v3 校验正逆向标记、双向弧链与跨句引用。
- 首个偏差返回 `E_ROUNDTRIP_MISMATCH`，携带 `format_version/sent_id/token_id/field/expected/actual`。

## 快照版本化

`data/config/*.json` 中三个段各自带有 `version` 与 `checksum`：

- `pos_tags`
- `deprels`
- `migration_policy`

加载规则：

- 任一段校验和不匹配：`E_SNAPSHOT_CORRUPT`。
- 版本回退：`E_SNAPSHOT_CORRUPT`，类别 `VERSION_ROLLBACK`。
- 同版本内容变化：`E_SNAPSHOT_CORRUPT`，类别 `UNVERSIONED_REPLACEMENT`。
- 版本递增：整份快照原子替换，并追加可追溯历史。
- 收缩后的 deprel 集合使已有弧非法：`E_LABEL_RETIRED`，返回全部 `sent_id/token_id/deprel`，且当前快照不被提交。

样本演示了 `v1 -> v2` 的整体替换；历史中三个段均变为版本 2。

## 资源上限与流式降级

`ResourceLimits` 支持：

- `max_total_nodes`
- `max_tokens_per_sentence`
- `mode = strict | stream`

策略：

- 单句 token 超限不可通过流式修复，始终 `E_RESOURCE_LIMIT`。
- `strict` 模式下总节点超限返回 `E_RESOURCE_LIMIT`，并提示 `degradation_available=stream`。
- 流式模式在总节点超限时继续逐句迁移，标记 `TOTAL_NODE_LIMIT_STREAMING`，记录触发句、此前已输出句数与节点数。
- 自检与测试验证流式输出每条弧的 `token_id/head/deprel` 与批处理完全一致。

## 领域接口

四个稳定入口均从 `dep_arc_interop` 导出：

```python
from dep_arc_interop import (
    SnapshotStore,
    load_format,
    migrate,
    judge_equivalence,
    verify_roundtrip,
)

store = SnapshotStore()
store.load("data/config/controlled_snapshot_v1.json")
snapshot = store.current

v1 = load_format("data/samples/arcs_v1.txt", snapshot)
v2 = load_format("data/samples/arcs_v2.txt", snapshot)
v3 = load_format("data/samples/arcs_v3.json", snapshot)

equivalence = judge_equivalence({"v1": v1, "v2": v2, "v3": v3}, snapshot)
upgraded = migrate(v1, "v3", snapshot)
downgraded = migrate(v3, "v1", snapshot)
roundtrip = verify_roundtrip("data/samples/arcs_v2.txt", snapshot)
```

异常统一为 `BusinessError`：

```json
{
  "ok": false,
  "error": {
    "code": "E_HEAD_RANGE",
    "message": "head must be 0 (virtual root) or an in-sentence token id",
    "details": {
      "field": "head",
      "sent_id": "B011",
      "token_id": 2,
      "head": 99
    }
  }
}
```

## 自检

`run_self_check` 检查三类一致性：

- `AUDIT_REPLAY`：9 个迁移方向均可由 `fills/drops` 重放出同一目标弧图。
- `FORMAT_VIEW_ARCS`：同一 `sent_id` 的 v1/v2/v3 弧集合、根和 deprel 可互核。
- `SNAPSHOT_TRACE`：POS、deprel、迁移策略的版本与校验和单调且可追溯。

任何不一致返回 `E_INCONSISTENT`，并指出 `category`、`sent_id`、`format_version`。注入样本 `selfcheck_inconsistent` 使用富 v3 触发该错误。

## 异常输入与业务码

`data/injection/cases.json` 声明 20 个场景，完整演示会逐项执行。覆盖：

- `E_VERSION_UNSUPPORTED`
- `E_FORMAT_CORRUPT`
- `E_DEPREL_INVALID`
- `E_HEAD_RANGE`
- `E_ROOT_VIOLATION`
- `E_CYCLE_DETECTED`
- `E_TOKEN_SEQ`
- `E_DUP_SENT`
- `E_POS_INVALID`
- `E_RESOURCE_LIMIT`
- `E_SEMANTIC_DRIFT`
- `E_ROUNDTRIP_MISMATCH`
- `E_SNAPSHOT_CORRUPT`
- `E_LABEL_RETIRED`
- `E_INCONSISTENT`

实际运行结果见 `artifacts/validation_report.json` 中的 `injection_results`。

## 目录结构

```text
dep_arc_interop/       可安装领域组件
scripts/               固定种子样本与注入集生成器
data/samples/          原始 JSONL、v1/v2/v3 同源样本
data/config/           受控集合与迁移策略快照、退役/损坏注入
data/injection/        格式、语义、资源、自检异常样本
tests/                 35 项 unittest 测试
examples/              最小集成示例与完整演示
artifacts/             实际验证报告
```

## 复现

```bash
.venv/bin/python scripts/generate_samples.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m dep_arc_interop.cli demo \
  --data-root data \
  --report artifacts/validation_report.json \
  --compact
```

生成器仅依赖固定种子和本地数据；连续运行两次产生相同内容。全部业务验证不访问网络、不访问未授权目标，也不使用容器环境。
