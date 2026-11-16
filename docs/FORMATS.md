# 数据与配置文件格式说明

所有文件均为 UTF-8 文本，路径一律用 `pathlib` 处理，相对路径相对于**配置文件所在目录**解析。

## 1. 查询样本 `data/queries.jsonl`

每行一个 JSON 对象：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query_id` | string | 是 | 查询 ID，全局唯一，用于派生稳定排序键 |
| `text` | string | 是 | 原始查询文本（注入畸形查询时由服务置为 `null`） |
| `context` | array[string] | 否 | 会话上下文，保留透传，不参与打分 |
| `label_intent` | string/null | 否 | 意图标注，用于报告中的 label_accuracy |

## 2. 意图词表 `data/intent_vocab.json`

```json
{
  "version": "1.0",
  "position_decay": "1/(1+pos)",
  "intents": [
    {"name": "purchase", "base_boost": 0.2,
     "features": [{"term": "买", "weight": 1.0}]}
  ]
}
```

- 原始得分 = Σ(命中特征 `weight` × 位置衰减 `1/(1+首次命中位置)`) + （有命中时）`base_boost` + 规则表 `intent_boost` 加分；
- 随后用 numpy 做 L2 归一化到 [0,1]，输出保留 6 位小数；
- 同分主意图按类目名称字典序；全零时 `no_signal=true`，主意图取字典序最小类目。

## 3. 实体词典 `data/entity_dict.json`

```json
{"dictionaries": [
  {"type": "brand", "priority": 1, "entries": ["苹果"]},
  {"type": "category", "priority": 2, "entries": ["手机"]},
  {"type": "attribute", "priority": 3, "entries": ["全新"]}
]}
```

- `priority` 数字越小优先级越高；
- 重叠候选消解顺序：最长优先 → `priority` 升序 → 起始位置升序 → 词条字典序（全序，无随机）；
- 被吞并候选记入 `suppressed`，其数量即 `E_SLOT_OVERLAP` 计数。

## 4. 改写规则表 `data/rewrite_rules.json`

```json
{"rules": [
  {"rule_id": "R_SYN_001", "kind": "synonym", "pattern": "购置", "replacement": "买",
   "benefit": 0.6, "intent_boost": {"intent": "purchase", "value": 0.3}, "requires": []},
  {"rule_id": "R_EXP_001", "kind": "category_expand", "pattern": "手机壳",
   "replacement": "防摔手机壳", "benefit": 0.4, "requires": ["dict:category:手机壳"]},
  {"rule_id": "R_CMP_001", "kind": "drop_complete", "trigger_slot": "brand",
   "missing_slot": "category", "replacement": "手机", "benefit": 0.8, "requires": []}
]}
```

- `kind`：`synonym`（同义归一，文本含 `pattern` 时替换）、`category_expand`（类目槽位等于 `pattern` 时扩展）、`drop_complete`（丢词补全：存在 `trigger_slot` 槽位且缺少 `missing_slot` 槽位时追加 `replacement`）；
- `requires` 支持 `rule:<rule_id>` 与 `dict:<type>:<entry>` 两种引用；引用缺失 → 输出 `E_RULE_MISSING` 并跳过该规则；
- `intent_boost` 为可选的意图加分（`pattern` 命中时生效）；
- 建议排序：预估收益 `benefit` 降序 → `rule_id` 字典序；最多输出 `max_rewrites` 条。

## 5. 注入配置 `data/injection.json`

```json
{
  "injection_id": "INJ-BASE-001",
  "malformed": {"ratio": 0.12},
  "dict_remove": ["手机壳"],
  "rule_conflicts": [["R_SYN_002", "R_SYN_003"]],
  "score_out_of_range": [{"query_id": "Q010", "intent": "purchase", "score": 1.75}]
}
```

- `malformed.ratio`：按 `sha256(query_id)` 确定性映射到 [0,1) 选取畸形查询（文本置 `null`），命中注入项 `INJ_MALFORMED`；
- `dict_remove`：词典条目移除清单，命中注入项 `INJ_DICT_REMOVE:<entry>`；可能导致规则 `requires` 失效 → `E_RULE_MISSING`；
- `rule_conflicts`：规则冲突对，同一查询同时命中时保留规则 ID 字典序较小者，输出 `E_RULE_CONFLICT`，命中注入项 `INJ_RULE_CONFLICT:<idA>|<idB>`；
- `score_out_of_range`：强制写入原始意图分，越界（<0 或 >1）时输出 `E_SCORE_OUT_OF_RANGE` 并截断到 [0,1]，命中注入项 `INJ_SCORE_OOR:<qid>:<intent>`；
- 注入配置文件的 sha256 记入报告 `injection_hash`，同配置可复现完全相同的失败清单与业务码序列。

## 6. 运行配置 `data/configs/*.json`

| 字段 | 说明 |
|---|---|
| `run_name` | 运行名 |
| `max_workers` | 线程池最大并发数（1 即串行） |
| `max_rewrites` | 每条查询最多输出的改写建议条数 |
| `snapshot_interval` | 每处理多少条样本写一次阶段快照 |
| `stop_after_stage` | 实验台用：完成该阶段后模拟强制终止（恢复运行时忽略） |
| `data.*` | 查询/词表/词典/规则/注入配置路径（`injection` 可为 `null`） |
| `snapshot_dir` / `report_path` | 快照目录与报告输出路径 |

- `config_hash` = sha256(配置文件 + 查询 + 词表 + 词典 + 规则表字节)；
- 快照含阶段名、`config_hash`、`injection_hash`、已处理游标 `cursor`、结果指纹 `result_fingerprint`；
- 恢复时任一哈希不符 → 输出 `E_CONFIG_MISMATCH` 并以退出码 2 拒绝恢复。

## 7. 业务码一览

`E_EMPTY_QUERY`（空/纯标点查询）、`E_MALFORMED_QUERY`（注入畸形查询）、`E_SLOT_OVERLAP`（槽位重叠计数）、`E_RULE_MISSING`（规则引用缺失）、`E_RULE_CONFLICT`（注入规则冲突被消解）、`E_SCORE_OUT_OF_RANGE`（注入评分越界被截断）、`E_CONFIG_MISMATCH`（快照哈希不符）、`E_INTERNAL`（未预期异常）。

## 8. 退出码

`0` 正常结束或收到可处理信号（SIGTERM/SIGINT）优雅退出（当前阶段快照已持久化）；`2` 配置哈希不符拒绝恢复；`9` 实验台模拟强制终止；`1` 其他错误。
