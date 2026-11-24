# 离线审计事件脱敏编辑组件

这是一个只依赖 Python 标准库与 pytest 的本地确定性实现，用于把 JSONL
审计事件流转换为脱敏后的结构化结果，并在 SQLite 中维护可追溯的字段级判定
账本。组件不访问网络、不依赖外部服务，也不使用容器。

已验证环境：Python 3.10.12 + pytest 8.2.0。实现保持 Python 3.10/3.11
兼容；目标 Windows 环境可直接使用 Python 3.11。

## 快速开始

```powershell
python -m pip install pytest
python -m audit_masker --version

$W = ".auditmask"
python -m audit_masker import-events --workspace $W --events samples/generated/events.jsonl
python -m audit_masker import-rules --workspace $W --snapshot samples/generated/rules_snapshot.json
python -m audit_masker recompute --workspace $W
python -m audit_masker export-report --workspace $W `
  --output out/report.json --graph out/graph.json `
  --truth samples/generated/truth.json
python -m audit_masker checkpoint create --workspace $W
python -m audit_masker checkpoint verify --workspace $W
```

重新生成固定种子样本：

```powershell
python samples/generate_samples.py --out samples/generated --seed 20261125
```

运行验证：

```powershell
python -m pytest -q
```

## 项目结构

```text
audit_masker/
  parsing.py      事件、v1/v2 规则、补丁的严格解析与规范化
  rules.py        两代规则合并、权威裁决、墓碑与同代冲突
  engine.py       字段/事件脱敏裁决与压制明细
  ledger.py       SQLite 模式、事务与账本读写
  service.py      增量重判闭包、布局重建、报告导出
  checkpoint.py   原子检查点、校验与恢复
  locking.py      POSIX/Windows 文件锁
  oracle.py       真值校验使用的独立小型参考实现
  cli.py          命令行入口
samples/
  generate_samples.py  固定种子样本与 marker truth 生成器
  generated/           已生成样本、真值、补丁与冲突样本
tests/                  pytest 验收测试
```

## 输入契约

### 事件 JSONL

每行一个 JSON 对象：

```json
{
  "event_id": "evt-000",
  "tenant": "tenant-0",
  "actor": "actor-0",
  "category": "contact",
  "ts_ms": 1760000000000,
  "seq": 0,
  "payload": {"email": "alice@example.com", "phone": "13800138000"},
  "idempotency_key": "idem-20261125-0000",
  "sync_marker": "SYNC-20261125-0000"
}
```

严格校验：

- `event_id` 在事件流内唯一；`seq` 为同一 `event_id` 内不重复的非负整数。
- `tenant/category` 为非空字符串，`actor` 为字符串。
- `ts_ms` 为非负整数，且明确排除 JSON 布尔值。
- `payload` 必须是字符串到字符串的对象。
- `idempotency_key` 为非空字符串。
- `sync_marker` 可选；存在时必须为非空字符串。

非法行产生带 `source_format`、`source_ref`、`record_index`、业务码和原始
记录的拒绝项，随后继续处理同一文件/同一批次后续记录。

### v1 规则

v1 行严格为四段：

```text
规则规范名|字段路径|掩码方式|门限
contact-email|payload.email|email|20
```

门限必须为非负十进制整数。v1 没有精度声明，也没有逐行生效时刻字段。
快照提供观测时钟 `observation_clock_ms`，并可通过以下任一形式提供与 v1
记录对齐的相对生效秒：

```json
{"v1_relative_seconds": [0, 5, 0]}
```

或：

```json
{
  "v1": [
    {"line": "r1|payload.email|email|20", "relative_effective_seconds": 5}
  ]
}
```

未声明时相对秒为 0。换算公式固定为：

```text
effective_ms = observation_clock_ms + relative_seconds * 1000
```

实现从不读取系统当前时间作为 v1 生效基准。v1 代数为快照声明的
`v1_generation`，缺省为 1。

### v2 规则

v2 是带字段名的键值对象。JSON 对象行：

```json
{
  "rule_name": "contact-email",
  "field_path": "payload.email",
  "mask": "hash",
  "threshold": 30,
  "effective_ms": 1760000010000,
  "generation": 2,
  "tombstone": false
}
```

也接受键值行：

```text
rule_name=contact-email;field_path=payload.email;mask=hash;threshold=30;effective_ms=1760000010000;generation=2;tombstone=false
```

`rule_name`、`effective_ms`、`generation`、`tombstone` 为必填。非墓碑规则还
必须提供 `field_path`、`mask`、`threshold`。墓碑记录可省略字段路径、掩码与
门限；快照示例仍保留完整字段，便于审计。

### 规则快照

```json
{
  "schema": "audit-mask-rules/v1",
  "snapshot_id": "baseline-20261125",
  "observation_clock_ms": 1760000000000,
  "v1_generation": 1,
  "v1_relative_seconds": [0, 5],
  "v1": ["contact-email|payload.email|email|20"],
  "v2": [
    {
      "rule_name": "contact-email",
      "field_path": "payload.email",
      "mask": "hash",
      "threshold": 30,
      "effective_ms": 1760000010000,
      "generation": 2,
      "tombstone": false
    }
  ],
  "category_defaults": {
    "contact": {
      "mask": "redact",
      "rule_name": "default:contact",
      "generation": 0,
      "fields": ["payload.email", "payload.phone", "payload.address"]
    }
  },
  "layout": {
    "mask_methods": ["keep", "redact", "email", "last4", "hash"],
    "threshold_mode": "higher_wins",
    "version": "layout-v1"
  }
}
```

`category_defaults` 的 `fields` 支持 `payload.email`、`payload.*` 和
`payload.cont*` 形式。默认处置规则名与代数会写入每个字段结论。未命中规则
且无类别默认时，字段保持原值，`basis` 为 `no_rule`。

### 补丁

补丁文件可为数组，或包含 `patches` 数组：

```json
{
  "patches": [
    {
      "patch_id": "patch-001-phone-upgrade",
      "changed_rules": ["contact-phone"],
      "tombstones": [],
      "v2": [
        {
          "rule_name": "contact-phone",
          "field_path": "payload.phone",
          "mask": "redact",
          "threshold": 35,
          "effective_ms": 1760000030000,
          "generation": 3,
          "tombstone": false
        }
      ]
    }
  ]
}
```

`patch_id` 非空且内容幂等：同 ID 同摘要重复导入会跳过；同 ID 不同摘要会报
`E_PATCH_DUPLICATE`。补丁只改变规则数据，不直接改账本。

## 统一判定视图

同一规范名的所有 v1/v2 记录先规范化，再按以下顺序确定权威记录：

1. 高 `generation`；
2. 较晚 `effective_ms`；
3. 同一代数与时刻时，墓碑优先；
4. 来源格式、来源路径与记录序号作为最终确定性排序键。

最高权威记录为墓碑时，该规范名整体失效，并覆盖较早的非墓碑记录。

同规范名、同代数、且非墓碑记录在 `(field_path, mask, threshold)` 上不一致
时，产生规则冲突。冲突报告列出每个来源的格式、路径、序号、字段值、掩码、
门限、生效时刻和原始记录。冲突规则不参与脱敏裁决；若事件字段受冲突影响，
字段业务码包含 `E_RULE_CONFLICT`。

字段同时命中多个有效规则时：

1. 门限高者胜；
2. 门限相同，代数高者胜；
3. 仍相同，生效时刻晚者胜；
4. 仍相同，规则名稳定排序决胜。

所有匹配规则进入 `matches`，未胜出规则同时进入 `suppressed`，并带
`lower_threshold`、`lower_generation`、`older_effective_time` 或
`rule_name_tiebreak` 原因。

## 掩码方法

当前布局白名单支持：

| 方法 | 结果 |
| --- | --- |
| `keep` | 原值 |
| `redact` | `***` |
| `email` | `a***@example.com` |
| `last4` | `***3456`；长度不超过 4 时为 `***` |
| `hash` | `sha256:` 加 SHA-256 前 16 个十六进制字符 |

统一视图指纹覆盖掩码白名单、门限口径版本、类别默认与完整规则状态。布局指纹
变化时，重判会在单事务内清空旧账本并整体重建，同时删除引用旧布局的检查点，
不会把新旧口径结论混在一起。

## SQLite 账本

默认数据库：`<workspace>/audit_masker.sqlite3`。

核心表：

- `events`：导入事件原始语义数据。
- `rule_records`：规范化后的 v1/v2 规则来源记录。
- `patches`：补丁声明与内容摘要。
- `ledger`：主键 `(event_id, seq, field_path)`，保存结论、掩码值、胜出规则名/
  代数/门限、依据、匹配/压制明细、布局指纹和判定水位。
- `event_results`：完整事件级 JSON 结论与业务码。
- `import_rejections`：解析拒绝记录。
- `checkpoints`：检查点元数据、水位、摘要和快照路径。

重判流程先在内存完成全部受影响事件裁决，再开启 `BEGIN IMMEDIATE` 单事务
写入。任何前置解析/计算错误都不会改动账本。事件集在计算与写事务之间发生
变化时，事务会回滚并拒绝半套结果。

## 增量重判闭包

重判以“上次已应用视图状态”与“当前统一视图”的差分为基础：

1. 找出权威状态或依赖路径发生变化的规则规范名；
2. 沿规则 → 字段路径 → 事件 payload 的依赖关系筛选事件；
3. 类别默认变化时筛选对应类别事件；
4. 并入尚未生成结论的新事件；
5. 全量布局重建时直接覆盖全部事件；
6. 仅删除并重写受影响事件的账本行；
7. 输出和账本均按 `(event_id, seq, field_path)` 稳定排序。

补丁只用其声明的规则名与最终视图决策，不把补丁到达序号作为权威顺序。因此
同一组乱序补丁在重判后得到相同结果；`verify-full` 可从零重算并与增量账本
逐字段比较。

## 检查点与恢复

`checkpoint create` 执行：

1. 读取全部已完成事件结果、账本行和摘要；
2. 构造含水位、布局指纹、规则指纹和完整账本行的快照；
3. 在快照同目录写临时文件、`fsync`、读取并校验 JSON 可解析，再原子替换；
4. 在 SQLite 事务中写入只引用完整快照的检查点行。

SQLite 行提交前若进程被强制终止，最多留下无索引的孤儿快照；验证会把它计入
`orphan_snapshots` 并忽略。若检查点行存在但快照缺失、摘要不符或账本摘要
不符，则列为无效检查点。恢复只选择最后一个完整且可验证的检查点，并通过
账本摘要重放确认一致性。若检查点之后已到达新规则但尚未重判，恢复仍可回到
最后完整账本，并返回 `W_RECOMPUTE_REQUIRED`；随后执行 `recompute` 会沿依赖
闭包补齐新规则影响。布局口径已变化时拒绝恢复旧口径账本。

`AUDIT_MASKER_FAULT=after_snapshot` 是用于故障注入测试的钩子：快照原子落盘
后、检查点行提交前立即 `os._exit(137)`，不生成退出报告。

同一 workspace 的导入、重判、报告导出与检查点写操作由跨平台文件锁
`.auditmask.lock` 串行化。锁被占用时命令返回业务码 `E_LOCK` 和退出码 1；
进程退出时操作系统自动释放锁。

## 同步标记真值与报告

样本生成器写入 truth：

- `sync_marker`；
- `event_id` 与 `seq`；
- 发送端原始 payload 的 UTF-8 字节十六进制；
- 原始 payload 字节的 SHA-256。

truth 不包含生成器算出的脱敏结论。报告校验只按 `sync_marker` 将组件输出与
truth 配对，再根据 truth 中的发送端原始字节和同步规则视图，通过
`oracle.py` 的独立小型参考实现重新计算期望值。报告中的
`inconsistent_count` 与每个事件业务码可用于验收。

报告为可解析 JSON，包含：

- 事件级状态与业务码；
- `masked_payload` 与逐字段结论；
- 每个字段的胜出规则名、代数、门限、依据、全部匹配和压制规则；
- 两代规则合并后的活动/墓碑/冲突规则；
- 导入拒绝、检查点摘要之外的诊断信息；
- truth 不一致计数与明细。

依赖图数据文件 schema 为 `audit-mask-dependency-graph/v1`，记录规则 → 字段
→ 事件键、类别默认与冲突。

## 命令与退出码

```text
python -m audit_masker import-events --workspace W --events FILE [FILE ...]
python -m audit_masker import-rules  --workspace W --snapshot FILE [--patch FILE ...]
python -m audit_masker recompute     --workspace W [--full]
python -m audit_masker export-report --workspace W --output FILE [--graph FILE] [--truth FILE]
python -m audit_masker checkpoint create  --workspace W [--snapshot FILE]
python -m audit_masker checkpoint verify  --workspace W
python -m audit_masker checkpoint restore --workspace W
python -m audit_masker verify-full   --workspace W
```

退出码：

- `0`：命令全部成功，无拒绝、冲突或真值不一致。
- `1`：部分失败、规则冲突、真值不一致、检查点无效或 `E_LOCK`；已成功的
  合法记录仍会保留。
- `2`：命令行参数错误。

主要业务码：

| 业务码 | 含义 |
| --- | --- |
| `E_PARSE` | JSON/格式解析失败 |
| `E_DUPLICATE_EVENT` | 重复事件键 |
| `E_DUPLICATE_EVENT_ID` | 事件流内重复 `event_id` |
| `E_PAYLOAD` / `E_TS_MS` / `E_SEQ` | 字段类型校验失败 |
| `E_V1_FIELDS` / `E_V2_FIELDS` | 两代规则格式非法 |
| `E_SNAPSHOT_EMPTY` | 快照全部规则均解析失败，拒绝替换既有规则 |
| `E_MASK_NOT_ALLOWED` | 掩码不在布局白名单 |
| `E_RULE_CONFLICT` | 同规则同代数字段值冲突 |
| `E_PATCH_DUPLICATE` | 同补丁 ID 内容不同 |
| `E_LOCK` | workspace 文件锁冲突 |
| `E_CHECKPOINT` | 检查点缺失/损坏/不可恢复 |
| `W_LEDGER_DIVERGED` | 当前账本与最新完整检查点摘要不同 |
| `W_RECOMPUTE_REQUIRED` | 已恢复/读取的账本视图落后于当前规则，需要重判 |
| `E_TRUTH_MISMATCH` | marker 真值对照不一致 |
| `E_FULL_INCONSISTENCY` | 增量账本与全量重算不一致 |
| `I_LAYOUT_REBUILD` | 触发布局指纹变化后的整体重建 |
| `I_SUPPRESSED_RULES` | 字段存在被压制规则 |
| `I_CATEGORY_DEFAULT` | 字段命中类别默认处置 |

## 本次实际验证

在项目根目录执行：

```text
python -m pytest -q
29 passed (本机重复运行约 5.9-6.3s)
```

覆盖点包括：

- 非法事件/规则继续批处理，来源格式与记录序号正确；
- v1 相对秒只按声明时钟换算；
- 两代规则合并、墓碑覆盖、同代冲突来源明细；
- 门限优先于代数、压制规则明细分层；
- 类别默认处置与规则/代数依据；
- 乱序事件、乱序补丁、补丁幂等；
- 增量账本与从零全量重判逐字段一致；
- 布局指纹变化整体重建且旧检查点被清理；
- SQLite 事务账本、全量一致性自检；
- 文件锁冲突返回 `E_LOCK`；
- 检查点原子写、篡改检测、恢复；
- 强制终止后忽略孤儿快照并恢复最后完整检查点；
- marker + 原始字节 truth 对照、JSON 报告与依赖图落盘。

本地样本实测结果：12 条事件、42 个字段、12/12 同步标记匹配、
`inconsistent_count=0`；补丁正序与逆序导入后账本逐字段一致；
全量一致性校验 0 差异。
