# 防火墙策略两代格式等价性审计器

本项目实现一个不依赖容器的本地确定性审计引擎：把 v1 单文件规则快照与
v2 分片 JSON + SQLite 元数据快照归一为规则视图，对同一批流量裁决任务分别
以 v1、v2 和统一视图裁决，对映射声明做双向对账，并输出可解析的 JSON 报告。

实现只使用 Python 标准库；`pytest` 仅用于测试。目标运行时为 Python 3.11+。

## 快速开始（Windows PowerShell）

```powershell
python --version
pip install pytest

python -m firewall_audit.samplegen --out samples --seed 20261125

python -m firewall_audit import-snapshot `
  --sample samples/sample_manifest.json `
  --db state/ledger.sqlite3 `
  --checkpoint state/checkpoint.json `
  --report reports/import.json

python -m firewall_audit adjudicate `
  --db state/ledger.sqlite3 `
  --checkpoint state/checkpoint.json `
  --report reports/equivalence.json `
  --jobs 4

python -m firewall_audit export-report `
  --db state/ledger.sqlite3 `
  --report reports/diagnostic.json

python -m firewall_audit verify-checkpoint `
  --checkpoint state/checkpoint.json `
  --db state/ledger.sqlite3 `
  --report reports/checkpoint.json `
  --require-match
```

映射补丁可直接基于当前账本应用，无需重传规则快照：

```powershell
python -m firewall_audit import-snapshot `
  --mapping-patch samples/incremental/mapping-patch-v4.json `
  --db state/ledger.sqlite3 `
  --checkpoint state/checkpoint.json `
  --report reports/import-patch.json
```

也可安装命令入口：

```powershell
pip install -e .
firewall-audit --help
```

## 目录

```text
firewall_audit/
  parser.py       严格解析、拒绝记录、v1/v2 规范化
  mapping.py      映射代数裁决、双向对账、歧义与环检测
  views.py        v1/v2/unified 规范视图
  engine.py       五元组命中、冲突策略、等价性裁决
  closure.py      规则→映射→case 增量失效闭包
  ledger.py       SQLite 账本、单 case 事务
  checkpoint.py   校验和检查点、账本恢复
  report.py       结构化报告
  cli.py          四类子命令
  samplegen.py    固定种子样本与真值生成器
samples/          生成样本
state/            账本与检查点
reports/          JSON 报告
tests/            pytest 验收
```

## 输入格式

### 样本清单

`sample_manifest.json` 是唯一入口，并声明唯一的观测时钟：

```json
{
  "format": "firewall-audit-sample",
  "schema_version": 1,
  "sample_id": "firewall-equivalence-20261125",
  "seed": 20261125,
  "observed_clock_ms": 1735689600000,
  "cases": "cases.jsonl",
  "truth": "truth.jsonl",
  "v1": "v1.json",
  "v2_manifest": "v2/manifest.json",
  "mapping": "mapping.json"
}
```

`observed_clock_ms` 是 v1 相对秒数的唯一换算基准。引擎不读取系统当前时间。

### case JSONL

每行必须含 `case_id`、`sync_marker`、`src_prefix`、`dst_prefix`、
`protocol`、`src_port_range`、`dst_port_range`。端口区间用闭区间字符串
`lo-hi`；协议白名单为 `tcp/udp/icmp/sctp/gre`。可选的 `rule_seq`、`action`
仅作为样本标注，不参与裁决，避免用期望值反向制造结果。

### v1 快照

```json
{
  "format": "v1",
  "snapshot_id": "v1-20261125",
  "generation": 1,
  "rules": [
    {
      "rule_key": "shared-web",
      "src_prefix": "10.20.30.77/24",
      "dst_prefix": "192.0.2.99/24",
      "protocol": "tcp",
      "src_port_range": "10000-20000",
      "dst_port_range": "80-80",
      "rule_seq": 10,
      "action": 1,
      "effective_relative_sec": 0
    }
  ]
}
```

v1 允许 CIDR 带主机位，解析时用 `strict=False` 归一为规范网段。动作码为
`0=deny, 1=allow, 2=log`。生效时刻为
`observed_clock_ms + effective_relative_sec * 1000`。

### v2 快照

`v2/manifest.json` 引用分片与元数据库：

```json
{
  "format": "v2",
  "snapshot_id": "v2-20261125",
  "generation": 2,
  "shards": ["shard-000.json", "shard-001.json"],
  "metadata_db": "meta.sqlite3"
}
```

分片行保存 `record_id/rule_key/五元组/rule_seq/action`；元数据库
`rule_meta` 保存 `generation/effective_ms/tombstone/CIDR 后缀/端口端点闭合
标记`。分片与元数据按 `record_id` 合并，元数据中的 rule_key 必须一致。

端口标记语义：

- `*_port_end_closed = 1`：`[start, end]`
- `*_port_end_closed = 0`：`[start, end)`，归一后上界为 `end-1`

因此 v1 的闭区间字符串与 v2 的闭开标记统一为 `[lo, hi]` 闭区间。

v2 基础地址必须是该 CIDR 后缀下的规范网络地址；例如 `/24` 必须写
`198.51.100.0`，带主机位会被拒绝。同一 `rule_key` 只允许在不同代数下存在
修订记录；相同 `(rule_key, generation)` 重复会报 `E_DUP_RULE_KEY`。

### 映射声明

```json
{
  "format": "mapping-declaration",
  "declaration_generation": 3,
  "direction": "v1_to_v2",
  "conflict_policy": "strict",
  "entries": [
    {"v1_rule_key": "shared-web", "v2_rule_key": "shared-web"},
    {"source_rule_key": "v1-alias-dns", "target_rule_key": "v2-alias-dns",
     "direction": "v1_to_v2", "mapping_generation": 3}
  ]
}
```

- `direction` 支持 `v1_to_v2`、`v2_to_v1`；显式 v1/v2 字段形式也可使用。
- 同一键出现多次时，对参与的 v1、v2 键分别取最高 `mapping_generation`。
- 同代的多目标/多来源变为 `E_MAP_AMBIGUOUS`。
- `active=false` 可表示更高代的撤回条目。
- 映射结论与条目到达顺序无关；重排不会改变规范指纹或触发重判。

## 规范化与裁决语义

### 权威记录

每个规范规则键先按以下元组取最大记录：

```text
(generation, effective_ms, tombstone, side_rank, record_id)
```

即规则代数优先，同代再按绝对生效时刻；同代较新的墓碑会覆盖更早的非墓碑记录。
墓碑键从活跃视图移除，但保留在 shadow 集用于 `E_TOMBSTONED` 诊断。

### 视图

- `v1`：只使用 v1 记录。
- `v2`：只使用 v2 记录，按 `rule_key` 选权威修订。
- `unified`：跨两代按同一 `rule_key` 选权威记录。
- v1/v2 等价性裁决分别使用各自视图；统一视图结果单独输出，不复用任一侧
  结果。

### 五元组命中

- 协议必须相同。
- case 的源/目的网段必须分别被规则网段包含（`case.subnet_of(rule)`）。
- 源端口区间与目的端口区间必须相交。
- 所有命中按最小 `rule_seq` 优先。

同一最小序号有多个规则时：

- 同一映射对且动作相同时视为一条逻辑规则，取权威记录。
- `strict`：返回 `E_RULE_CONFLICT`。
- `deny_wins`：allow 与 deny 并存时仅保留 deny；仍多于一条则冲突。
- `prefer_v2` / `prefer_v1`：优先指定侧；仍多于一条则冲突。

### 等价理由

每个 case 输出：

- `same_rule_key`：两侧命中键相同。
- `mapping_covered`：不同键由映射声明配对。
- `same_semantics`：五元组、序号和动作完全一致。
- `no_match`：两侧均无命中。
- `same_hit_keys`：两侧冲突命中集合相同。
- 其余不一致返回 `E_NOT_EQUIVALENT`，并给出两侧动作差异与命中键。

## 增量失效闭包

导入新规则快照或映射补丁时，先比较：

- `rule_generation`
- `mapping_generation`
- 规范规则集合指纹
- 规范映射集合指纹（含方向与冲突策略）

然后构建双向依赖图：

- 规则节点 ↔ 映射节点（映射对）
- 规则节点 ↔ case 节点（旧/新视图中可能命中的规则）

从规则记录变更、映射变更、case 内容变更和冲突策略变更出发 BFS，得到唯一
需要重判的 case 集合，按 `case_id` 排序。只有该集合进入 SQLite 重算；
未受影响 case 的账本结果保持不变。映射图有向环时输出 `E_MAP_CYCLE` 和闭环
键。`samples/incremental/sample_manifest.json` 只新增一个映射对，闭包应只
包含 `case-semantic`。

`import-snapshot --mapping-patch FILE` 会把补丁条目与当前账本映射合并；补丁
代数不得低于当前映射代数。需要撤回旧条目时写入更高代的 `active=false` 条目。

## 事务、检查点与恢复

- `import-snapshot` 在解析全部完成后才打开账本；解析失败不会创建或修改账本。
- 快照替换在一个 `BEGIN IMMEDIATE` 事务中完成，异常整体回滚。
- 每条 case 结论及其命中键在单独事务中删除旧值并写入新值。
- 检查点包含完整账本表、序号、状态指纹和 SHA-256 校验和。
- 报告和检查点都先写同目录临时文件、`fsync`，再 `os.replace` 整体替换。
- 强杀后若账本与最后检查点不一致，执行
  `verify-checkpoint --restore` 可事务化恢复到最后完整检查点。强杀时不保证
  生成退出报告。

测试专用故障注入：

```powershell
$env:FIREWALL_AUDIT_TEST_KILL_AFTER_CASE_COMMITS = "1"
python -m firewall_audit adjudicate ...
```

该环境变量仅用于验证“提交 N 条后强制退出”的恢复边界。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_BAD_SCHEMA` | 结构或类型非法 |
| `E_DUP_CASE_ID` | case_id 重复 |
| `E_DUP_RULE_KEY` | 同代规范规则键重复 |
| `E_DUP_RECORD_ID` | v2 record_id 重复 |
| `E_BAD_PREFIX` / `E_BAD_CIDR_SUFFIX` | CIDR 或后缀非法 |
| `E_BAD_PROTOCOL` | 协议不在白名单 |
| `E_BAD_PORT_RANGE` / `E_BAD_PORT_MARKER` | 端口区间或闭开标记非法 |
| `E_BAD_EFFECTIVE_TIME` | 秒/毫秒时刻非法 |
| `E_BAD_GENERATION` | 规则/映射代数非法 |
| `E_BAD_ACTION` / `E_BAD_RULE_SEQ` | 动作或规则序号非法 |
| `E_META_MISSING` / `E_META_INVALID` / `E_META_ORPHAN` | v2 元数据关联异常 |
| `E_RULE_KEY_MISMATCH` | 分片与元数据规则键不一致 |
| `E_MAP_MISSING` | 活跃 v1 规则无存在的 v2 键对应（墓碑键仍算存在） |
| `E_MAP_EXTRA` | 活跃 v2 规则无活跃 v1 对应 |
| `E_MAP_AMBIGUOUS` | 一对多或多对一 |
| `E_MAP_CYCLE` | 映射依赖有向环 |
| `E_NO_MATCH` | 无规则命中 |
| `E_RULE_CONFLICT` | 最小规则序号冲突未消解 |
| `E_TOMBSTONED` | 命中的权威记录是墓碑 |
| `E_NOT_EQUIVALENT` | 两侧结论不等价 |
| `E_CHECKPOINT_INVALID` / `E_CHECKPOINT_MISMATCH` | 检查点损坏或账本不匹配 |
| `E_SNAPSHOT_MISMATCH` / `E_NO_SNAPSHOT` | 样本指纹不一致或未导入快照 |

拒绝记录固定包含 `source_category`、`record_index`、`code`、`message`，并尽量
附带 `rule_key` 或 `case_id`。

## 测试

```powershell
pip install pytest
pytest -q
```

测试覆盖真值原始字节、确定性生成、严格拒绝、映射顺序无关/代数/环、墓碑、
同序号冲突、并发一致、增量闭包与全量逐字段一致、原子报告、检查点校验和、
强制终止恢复。实际验证结果见 `VALIDATION.md`。
