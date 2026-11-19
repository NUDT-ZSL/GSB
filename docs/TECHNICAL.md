# 技术说明

## 1. 架构

| 模块 | 职责 |
|---|---|
| `annotation_app/model.py` | v1/v2 数据类、规范化序列化、事件图快照与差异 |
| `annotation_app/reader.py` | 版本头分派、严格 JSON 读取、字段/偏移/跨度/引用校验 |
| `annotation_app/migration.py` | v1↔v2 迁移、规范化标识补全、有损降级明细 |
| `annotation_app/lineformat.py` | 行式格式导出/导入与前缀声明 |
| `annotation_app/consistency.py` | 事件图不变式、跨模块核对、论元追溯 |
| `annotation_app/editing.py` | 事务式编辑与编辑日志 |
| `annotation_app/audit.py` | 审计 JSONL 与重放校验 |
| `annotation_app/ontology.py` | 受控集合组件版本、校验和、整体替换与收缩检测 |
| `annotation_app/config.py` | 版本化运行配置与资源阈值 |
| `annotation_app/resources.py` | 阈值拒绝或仅保留实体层降级 |
| `annotation_app/storage.py` | 原子写、过期临时文件清理 |
| `annotation_app/service.py`, `web.py` | 本地服务与零依赖 Web UI |
| `annotation_app/demo.py` | 可重复端到端审计报告 |

所有路径均使用 Python `str` 索引，即 Unicode 码点位置；不使用 UTF-16 或字节偏移。

## 2. 规范格式

### v1 扁平 JSON

```json
{
  "version": "v1",
  "sample_id": "sample-20241120",
  "documents": [{
    "doc_id": "doc-1",
    "text": "……",
    "entities": [{"id": "e1", "type": "PERSON", "start": 0, "end": 2, "text": "赵磊"}],
    "relations": [{"id": "r1", "type": "WORKS_AT", "head": "e1", "tail": "e2"}],
    "triggers": [{"id": "t1", "type": "JOIN", "start": 10, "end": 12, "text": "加入"}],
    "arguments": [{"id": "a1", "trigger_id": "t1", "entity_id": "e1", "role": "employee"}]
  }]
}
```

v1 不含显式事件对象，每个触发词对应一个隐式事件，迁移时生成 `evt:<trigger-id>`。

### v2 统一事件图

```json
{
  "version": "v2",
  "sample_id": "sample-20241120",
  "documents": [{
    "doc_id": "doc-1",
    "text": "……",
    "nodes": [
      {
        "id": "e1", "kind": "entity", "type": "PERSON",
        "start": 0, "end": 2, "text": "赵磊",
        "canonical_id": "person:赵磊",
        "confidence": {"low": 0.96, "high": 0.99}
      },
      {
        "id": "t1", "kind": "trigger", "type": "JOIN",
        "start": 10, "end": 12, "text": "加入",
        "event_id": "evt-hire-1",
        "confidence": {"low": 0.91, "high": 0.97}
      }
    ],
    "edges": [
      {
        "id": "r1", "kind": "relation", "type": "WORKS_AT",
        "source": "e1", "target": "e2",
        "confidence": {"low": 0.90, "high": 0.97}
      },
      {
        "id": "a1", "kind": "argument", "role": "employee",
        "source": "e1", "target": "t1", "event_id": "evt-hire-1",
        "confidence": {"low": 0.86, "high": 0.95}
      }
    ]
  }],
  "coref": [{
    "id": "c1", "canonical_id": "org:星河科技", "members": ["e2", "e4"]
  }]
}
```

语义约定：

- 关系边两端必须均为实体节点。
- 论元边从实体指向触发词；`event_id` 必须等于目标触发词的 `event_id`。
- 每个触发词有且仅有一个非空事件归属；一个事件可拥有多个触发词。
- 共指成员必须解析为实体节点，并且不得跨组重复。
- 边只在所属文档内解析。

## 3. 错误契约

每条错误均包含 `code`、`message`、`location`、`details`、`attributed`。`location` 可含阶段、格式、版本、字段路径、文档、标注 ID 与行号。

| 错误码 | 触发条件 |
|---|---|
| `E_SCHEMA_MISSING` | 版本头、必填字段、共指/前缀/置信区间字段缺失 |
| `E_SCHEMA_TYPE` | 字段类型、事件/节点/边种类或置信区间不合法 |
| `E_OFFSET_TYPE` | 偏移不是非布尔整数 |
| `E_SPAN_INVALID` | `start >= end`、越界或文本切片不一致 |
| `E_DUP_ID` | 实体/触发词/边/文档/共指组 ID 重复 |
| `E_FORMAT_INVALID` | JSON、行式转义、前缀声明或请求格式错误 |
| `E_VERSION_UNSUPPORTED` | 未知版本头或快照 schema |
| `E_ROUNDTRIP_MISMATCH` | 导出导入后事件图/偏移/角色等不一致 |
| `E_DANGLING_REF` | 关系、论元或编辑目标引用不存在 |
| `E_ROLE_INVALID` | 论元角色不在活动受控集合，或无法按策略映射 |
| `E_TRIGGER_CONFLICT` | 触发词无事件归属、拆分/合并事件冲突 |
| `E_COREF_UNRESOLVED` | 共指引用无法解析或删除后失效 |
| `E_ONTOLOGY_CONTRACT` | 校验和、版本单调性、组件升级或严格收缩违约 |
| `E_INCONSISTENT` | 跨模块事件图、审计重放、论元追溯不一致 |
| `E_RESOURCE_LIMIT` | 导入字节、实体+论元总数或图节点数超限 |

解析器在成功后仍调用引用校验；任一失败即抛出错误，不产生部分对象。

## 4. 迁移规则

### v1 → v2

- 实体类型与关系类型按活动受控集合校验。
- `canonical_id` 默认按 `lower(type):NFKC(text).casefold()` 生成；快照支持显式覆盖。
- 论元角色必须属于受控集合；`role_map` 可声明显式映射，例如 `worker → employee`。无映射时返回 `E_ROLE_INVALID`。
- 每个 v1 触发词生成隐式事件 `evt:<trigger-id>`。
- 节点与边置信区间由迁移策略声明，默认 `[1.0, 1.0]`，并登记为 synthesized。
- 关系与论元分别进入统一事件图的 `relation`、`argument` 边。

### v2 → v1

快照必须声明：

```json
{
  "v2_to_v1": {
    "coref": "drop",
    "multi_trigger_event": "split",
    "confidence": "drop",
    "canonical_id": "drop"
  }
}
```

- 跨句共指组按 `drop` 规则移除，逐组登记。
- 节点/边置信区间按 `drop` 规则移除，逐条登记。
- 实体规范化标识按 `drop` 规则移除，逐实体登记。
- 多触发词事件按 `split` 规则变为每个触发词一个 v1 隐式事件；事件与论元归属差异逐项登记。
- 迁移器会执行一次确定性 v1→v2 重升级，并输出所有事件图差异，保证差异可审计。

## 5. 行式交换格式

首行固定为 `#LEDGER-LINE|1`，随后声明类型前缀：

```text
#PREFIX|entity|E
#PREFIX|trigger|T
#PREFIX|relation|R
#PREFIX|argument|A
#PREFIX|document|D
#PREFIX|meta|M
```

记录字段：

```text
E|doc_id|id|type|start|end|text|canonical_id|low|high|coref_id
T|doc_id|id|type|start|end|text|event_id|low|high
R|doc_id|id|type|start|end|text|source|target|low|high
A|doc_id|id|role|start|end|text|source|target|event_id|low|high
D|doc_id|text
M|sample_id|source_version|ontology_version|ontology_checksum
```

- 分隔符为 `|`。
- 转义：`\\`、`\|`、`\n`、`\r`、`\t`。每条物理记录只占一行。
- 关系行的跨度是两端实体跨度的并集；论元行的跨度取自源实体。
- 导入时先按声明前缀分派，再校验边行跨度/文本与引用节点一致，最后交给 v2 读取器全量校验。
- 往返比较包括事件图、码点偏移、角色、共指、规范化标识与置信区间。

## 6. 编辑与审计

支持：`add_entity`、`delete_entity`、`add_argument`、`delete_argument`、`change_argument_role`、`split_event`、`merge_events`。

事务流程：

1. 克隆当前 v2 状态。
2. 在克隆上执行操作。
3. 校验全部引用、角色、事件归属、共指和资源上限。
4. 成功才替换当前状态；失败只追加 `rejected` 审计。

审计条目：

```json
{
  "seq": 1,
  "operation": "change_argument_role",
  "payload": {"argument_id": "a1", "role": "employer"},
  "status": "applied",
  "before_checksum": "sha256:...",
  "after_checksum": "sha256:..."
}
```

重放会校验序号连续、前一条 `after_checksum` 等于后一条 `before_checksum`，并只重放 `applied` 条目；最终状态必须与记录状态完全一致。

## 7. 受控集合与配置版本

快照包含：

```json
{
  "version": 2,
  "checksum": "sha256:...",
  "entity_types": [],
  "argument_roles": [],
  "relation_types": [],
  "migration_policy": {},
  "components": {
    "entity_types": {"version": 2, "checksum": "sha256:..."},
    "argument_roles": {"version": 2, "checksum": "sha256:..."},
    "relation_types": {"version": 2, "checksum": "sha256:..."},
    "migration_policy": {"version": 2, "checksum": "sha256:..."}
  }
}
```

- 聚合版本与四个组件版本都必须单调递增。
- 同一版本内容变化、校验和不符均返回 `E_ONTOLOGY_CONTRACT`。
- 新版本以完整对象替换旧快照，不执行嵌套字段增量修补。
- 收缩影响现有标注时：`strict` 返回受影响清单并阻断；`lenient` 激活新快照并返回清单，由调用方决定后续修复。

## 8. 资源与归因

`snapshots/config-v1.json` 声明：

- `max_import_bytes`
- `max_annotation_total`：单样本实体 + 论元总数
- `max_graph_nodes`
- `on_limit`：`reject` 或 `entities_only`

`entities_only` 降级会移除触发词、边和共指组，仅在 `metadata.degradation` 中保留类别、阶段、违规项与移除计数。编辑路径不允许降级，超限直接拒绝。

错误归因字段包括：

- 解析：`stage=parse`、`format=json|line`、`version=v1|v2`、字段路径/行号。
- 迁移：`stage=migration`、源版本、`format=json`、字段路径。
- 编辑：`stage=edit`、`version=v2`、`format=v2`、操作与标注 ID。
- UI 服务和演示未知异常写入报告的 `unattributed` 段；演示正常路径必须为空。

## 9. 崩溃恢复

`storage.atomic_write_bytes` 使用同目录临时文件写入、`flush`、`os.fsync`、`os.replace` 和目录 `fsync`。若进程在替换前被强制终止：

- 活动文件保持上一完整版本。
- 只遗留 `.*.tmp` 临时文件。
- 下次 demo/UI 启动调用 `cleanup_stale_temps` 清理。

自动化测试通过子进程写半截临时文件后 `kill()`，验证原活动文件仍可解析。

