# 技术说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `models.py` | 规范化类型集、别名、标注记录、迁移结果和确定性 JSON/哈希 |
| `typesets.py` | 三格式分派、严格读取、字段/行号错误定位 |
| `migration.py` | 任意 v1/v2/v3 目标迁移、补全/丢弃明细、回读重放 |
| `config.py` | 合并、别名、停用替代、降级和资源策略校验 |
| `annotations.py` | JSONL 读取、文档/标注者集合和 token 序列一致性 |
| `merge.py` | 别名归一、停用类型裁决、投票、BIO 校验、资源限制 |
| `trace.py` | 只依赖原始票、标注者集合、类型集版本和配置的独立重放 |
| `semantic.py` | 三视图语义与合并结果等价裁决 |
| `selfcheck.py` | 文档集合、逐 token 标签、独立投票重算、跨视图自检 |
| `snapshots.py` | 版本化、校验和、原子整体替换、回退防护 |
| `api.py` | 可集成的领域门面 |
| `cli.py` | 本地完整示例与 JSON 报告 |

## 2. 规范化模型

`TypeSet` 是唯一语义来源：

- `label_set_version`
- 按名称排序的 `TypeDefinition`
- 每个类型下按名称排序的 `AliasDefinition`
- `format_version`、源路径、源行和迁移补全记录不参与语义指纹

语义指纹为规范化 JSON 的 SHA-256。启用集合、停用类型、BIO 要求、别名启用状态、目标类型和别名元数据均参与比较，因此跨格式比较不会只检查“能合并”而漏掉受控集合差异。

## 3. 读取与错误定位

分派顺序：

1. 文本以 `{` 开头：v3 JSON；语法错误直接 `E_TYPE_SET_CORRUPT`。
2. 首行 `#NER_TYPE_SET|...`：严格解析版本与 `label_set_version`。
3. 无抬头：JSON 字段集推断 v3；全 `key=value` 推断 v2；`TYPE`/`ALIAS`/`META` 行推断 v1。
4. 其余情况：`E_VERSION_UNSUPPORTED`。

解析采用“先构造、后返回”：任何数据行错误都会立即抛出，调用方拿不到部分对象。v1 竖线数列、v2 缺失块字段、v3 必填字段和不可解析启用标记均带 `field`、`line`、`reason`。

## 4. 迁移可重放性

迁移过程：

1. 深拷贝源类型集。
2. 根据目标格式补全默认值或应用降级策略。
3. 生成目标文本。
4. 立即用正式读取器回读。
5. 比较“预期目标语义指纹”和“回读指纹”。

补全和丢弃项均保留：

- `item`：规范化路径。
- `source_line`：源文件行号。
- `source` 或 `reason`。
- `target_format`。
- `lossy`：是否改变语义。

v1 的隐含语义是 `bio_prefix_required=true`、别名 `enabled=true`、`metadata={}`。因此 v1 升级时这些值成为显式字段并记录补全来源。v1 无法表达的字段由 `downgrade_policy` 明确决定丢弃或展平，禁止静默处理。

## 5. 合并算法

### 5.1 规范化

每个原始标签先拆成 BIO 前缀与类型：

1. `O` 保持。
2. 验证 `B-`/`I-` 与非空合法类型名。
3. 先查配置别名与类型集别名。
4. 别名目标为启用类型：替换为规范类型。
5. 别名本身禁用：`E_ALIAS_UNRESOLVED`。
6. 类型或别名目标为停用类型：进入停用裁决链。
7. 未知类型：`E_TYPE_NOT_ALLOWED`。

### 5.2 多数投票

按规范化标签分组。候选排序键为：

```text
(-count, support_annotators[0], label)
```

因此同票时，支持该标签的最小 `annotator_id` 较小者胜出；该键同时作为最后的稳定兜底。

### 5.3 优先级

按 `annotator_priority` 的索引排序；未列入配置的标注者使用 `len(priority)` 作为排名，再按 `annotator_id` 字典序。记录中保留是否使用了优先级兜底。

### 5.4 输出确定性

- 文档按 `doc_id` 排序。
- 标注者按 `annotator_id` 排序。
- token 位置按整数升序。
- 计数、分类、原因和哈希输入均显式排序。
- 不写入当前时间、随机数或运行路径参与哈希。

## 6. 停用类型裁决

停用类型必须仍出现在当前类型集中且 `enabled=false`，才能与未知类型区分：

- `substitute`：必须存在启用的替代类型；最终标签前缀保持不变，输出 `E_TYPE_RETIRED` 告警。
- `reject`：首个命中位置返回 `E_TYPE_RETIRED`。
- `doc_reject`：首个命中位置返回 `E_DOC_REJECTED`。

文档和合并摘要均包含：

- `processing_categories`
- `fallback_reasons`
- `handling_summary.category_counts`
- `handling_summary.fallback_reason_counts`

## 7. BIO 校验

最终标签逐位置检查：

- `O` 合法。
- 非 `O` 必须为 `B-TYPE` 或 `I-TYPE`。
- 类型必须在启用集合。
- `I-TYPE` 的前一最终标签必须是 `B-TYPE` 或 `I-TYPE`。

校验失败带 `doc_id`、`position`、`label`。

## 8. 追溯与重放

每个 token 的投票记录包含：

- `label_set_version`
- 全部原始票：`annotator_id`、`annotation_version`、`raw_label`
- 规范化标签与别名
- 聚合候选、票数、支持者集合
- 选中规则、同票信息和 `trace_id`
- 停用类型处理类别与原因

`trace_label` 不信任已存候选统计，而是从原始 JSONL 的 `ner_tags` 重新规范化、重新选择，并比较：

- 标注者集合
- 每条原始票
- 规范化标签
- 独立重算的候选统计
- 选择元数据
- 记录标签与最终标签

快照降级若省略 `choices`，播放器仍可用原始票重算；若原始票、标注者集合、版本、配置或任何一致性字段缺失/不符，则返回 `E_TRACE_BROKEN` 并指出 `doc_id` 与 `position`。

## 9. 跨格式与自检

`compare_format_views` 先比较完整语义视图，再分别合并并比较最终标注和完整合并摘要。任何不一致返回 `E_SEMANTIC_DRIFT`。

`run_self_check` 检查：

- 每个标注者的 `doc_id` 集合相同。
- 同一文档 token 序列和标签长度相同。
- 每个位置恰有一个最终标签和一条投票记录。
- 每个位置可由原始 `ner_tags` 独立重算。
- 所有追溯可重放。
- 三格式启用/停用集合、别名映射和合并结论相同。

自检阶段的跨模块不一致统一返回 `E_INCONSISTENT`，并包含第一个相关 `doc_id`。

## 10. 快照语义

快照结构：

```json
{
  "schema": "ner-governance-snapshot-v1",
  "kind": "type_set",
  "version": 2,
  "checksum": "<sha256>",
  "replaces_version": 1,
  "payload": {}
}
```

校验和覆盖 schema、kind、version 与 payload。读取时重算并比较。每次写入：

1. 先完整读取并校验当前快照。
2. 检查可选 `base_version` 与 `expected_checksum`，防止并发旧写入。
3. 产生 `version+1`。
4. 写入临时文件并 `fsync`。
5. 使用 `os.replace` 原子整体替换。

因此没有任何增量修补接口。以下情况分别拒绝：

- `base_version` 小于当前版本：`E_SNAPSHOT_ROLLBACK`。
- 恢复版本不大于当前：`E_SNAPSHOT_ROLLBACK`。
- JSON、字段、版本、kind、校验和或候选快照损坏：`E_SNAPSHOT_CORRUPT`。

## 11. 资源限制

- `max_types`：统计当前类型集全部类型（含停用）；超限返回 `E_RESOURCE_LIMIT`。
- `max_tokens_per_doc`：严格集合超限返回 `E_RESOURCE_LIMIT`；`resource_policy=degrade` 时截断并标记 `token_limit_truncated`。
- `max_resident_votes`：统计每个位置聚合后的候选标签条目数；严格集合超限返回 `E_RESOURCE_LIMIT`，降级时省略聚合候选并标记 `vote_details_omitted`，原始票保留以便重放。

## 12. 已知边界

- v1 是损失性较弱的旧格式：BIO 要求与别名启用状态由格式隐含；因此从 v1 升级必须记录默认补全。
- v1 和 v2 不能表达嵌套别名元数据；降级结果是否可接受由显式策略决定。
- 当前样本所有类型均要求 BIO 前缀。若某类型配置为 `bio_prefix_required=false`，降级到 v1 必须选择 `flatten` 或 `reject`。
- 当前执行机是 WSL2/Linux，不是原生 Windows；代码和输出不含平台专属路径依赖。
