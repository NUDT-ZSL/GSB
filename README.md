# 多级目录配额诊断组件

这是一个仅依赖 Python 标准库的可集成诊断组件，针对多级目录配额、两代规则快照、
增量失效闭包和中断恢复场景。实现只使用注入的 `clock_ms` 逻辑时钟，不读取系统墙钟。

## 快速开始

环境要求：Linux（WSL Ubuntu）、Python 3.10+、SQLite 标准库。无需 Flask 或网络服务。

```bash
# 1. 固定种子生成样本
python3 scripts/gen_seed.py --out artifacts/verification/data --seed 42

# 2. 完整端到端演示（会重新生成 artifacts/verification）
python3 scripts/run_demo.py --out artifacts/verification --seed 42

# 3. 自动化测试
python3 -m unittest discover -s tests -v
```

也可以安装为命令：

```bash
python3 -m pip install -e .
quota-diag --help
```

## CLI

```bash
quota-diag load    --db q.db --dirs data/dirs.json --writes data/writes.jsonl \
                   --v1 data/rules_v1.json --v2 data/rules_v2
quota-diag merge   --db q.db
quota-diag write   --db q.db --block-size 25
quota-diag patch   --db q.db --patch data/patch.json
quota-diag snapshot --db q.db --v2 data/rules_v2_next
quota-diag resume  --db q.db --block-size 25
quota-diag status  --db q.db
quota-diag verify  --db q.db
```

所有命令输出确定性 JSON；`--output FILE` 可同时落盘。业务/校验错误写入 stdout：

```json
{
  "error": {
    "code": "E_VERSION_ROLLBACK",
    "message": "rule snapshot version rollback is rejected",
    "fields": {
      "current_version": "v2-0002",
      "incoming_version": "v2-0001"
    }
  }
}
```

`snapshot` 是 `load/merge/write/patch/resume/status/verify` 之外补充的规则包入口；
只要求题目列出的七个命令时，可把它视为规则更新进程的独立操作。

## 输入契约

### 目录清单 `dirs.json`

```json
{
  "dirs": [
    {
      "dir_id": "d0",
      "parent_id": null,
      "base_quota": 6619,
      "ext_quotas": {}
    },
    {
      "dir_id": "d5",
      "parent_id": "d1",
      "base_quota": null,
      "ext_quotas": {"alpha": 3071, "beta": 2377}
    }
  ]
}
```

- `dir_id` 唯一；父目录必须存在且链上无环。
- `base_quota` 为正整数，或由非空 `ext_quotas` 按文件键声明正整数配额。
- 两者同时存在时，扩展配额优先于基础配额。

### 写入流 `writes.jsonl`

```json
{"seq":1,"clock_ms":0,"dir_id":"d7","file_key":"beta","bytes":32,
 "digest":"ad33...","content_b64":"..."}
```

- `seq` 在文件顺序上严格递增；`clock_ms` 非负且单调不降。
- 提供 `content_b64`、`content`、`payload` 等内联内容时，校验 UTF-8/原始字节长度和
  SHA-256 摘要；只提供摘要的旧格式输入仍可解析，但只能做摘要格式校验。
- 固定种子生成器始终写入 `content_b64`，因此样本可验证“摘要-字节数-内容”一致。

### v1 规则 `rules_v1.json`

`effective_in_seconds` 相对于 `base_clock_ms`，转换标度为 1000。小数毫秒采用向下取整，
并在合并视图记录 `effective_at_ms`、`scale`、`rounding`、`remainder_ms`。

### v2 规则包

目录包含 `meta.db` 和若干分片 JSON：

```text
rules_v2/
  meta.db
  shard_0.json
  shard_1.json
```

`meta.db`：

```sql
CREATE TABLE manifest (version TEXT, min_generation INTEGER, key_digest TEXT);
CREATE TABLE shards (name TEXT, sha256 TEXT);
```

加载时校验分片存在性、逐字节 SHA-256、分片内规则 schema，以及
`sha256("\n".join(sorted(rule_keys)))` 键集合摘要。

## 核心语义

### 跨版本合并

同一 `rule_key` 的候选规则按以下元组取最大者：

```text
(generation, effective_at_ms, source_priority, source_version)
source_priority: v1=1, v2=2, patch=3
```

因此规则代数优先；代数相同比较绝对逻辑时钟；仍相同则 v2 优先于 v1。每个统一规则节点
记录 `source`、`source_version`、`decision` 和 v1 换算信息。候选先分组再排序，
输入加载顺序或分片顺序不会改变结果。

### 配额账本与回滚

账本按 `(dir_id, file_key)` 保存 `consumed`、`remaining`、`quota` 和最后更新事件序号。
一次事件从目标目录开始向父链逐层扣减：

1. 先计算整条链是否均满足配额；
2. 任一节点配额不足或在扩展配额目录中缺少文件键，立即记录 `fail_dir`/`reason`；
3. 已成功的子目录尝试标记为 `rolled_back`，不提交任何扣减；
4. 全链成功时一次性更新所有层级。

事件审计、账本更新、已处理序号和三个水位在同一 SQLite 事务中提交。`verify` 会比较：

- 持久化账本与当前规则下全流模拟账本；
- 当前审计与逐事件模拟裁决；
- `SUM(ledger.consumed)` 与所有成功审计的扣减字节总和。

### 失效闭包

补丁支持：

- `rule_change`：提交新规则节点；
- `rename`：目录显示名变化；
- `tombstone`：规则墓碑或目录墓碑。

闭包起始于规则/重命名/墓碑目录，包含其子树和祖先链。由于共享祖先的扣减可能因某个
子树的成功/失败切换而变化，组件再用“旧规则全流模拟 vs 新规则全流模拟”的确定性 diff
补齐级联目录，例如兄弟子树。只有闭包内目录的账本行和相交路径的审计行会被持久化重算；
其他目录保持原账本和审计。`invalidation.recomputed=1` 表示该目录已完成重算。

### 快照切换

按版本号和内容签名判定：

- 同版本且签名相同：`no_op`；
- 同版本但签名不同：`E_SNAPSHOT_CONFLICT`；
- 版本更低：`E_VERSION_ROLLBACK`；
- 新版本保留旧键且 `min_generation` 不降低：`incremental_append`；
- 其他新版本：`full_replace`。

新快照先写入连接级临时表 `rules_v2_staging`，再写入版本化表并切换
`current_v2_snapshot` 指针；旧版本行保留用于诊断，版本回退不会原地覆盖。快照切换、
统一视图、失效标记和重算处于同一事务。

### 并发、版本绑定与恢复

- 写入进程共享 `<db>.lock`；规则替换/补丁使用排他锁。SQLite WAL +
  `BEGIN IMMEDIATE` 保证同一事件序号只提交一次。
- 每个写入块在事务内读取一次 `current_rules_version` 和统一视图，块内事件绑定同一版本。
- 默认每个块原子提交；`--block-size` 可调。`--crash-at-seq N` 是测试钩子，在 N 的
  扣减写入后直接退出，不执行 COMMIT。
- `resume` 只选择 `write_events LEFT JOIN processed_events` 中未处理的事件。
- 规则合并、快照、补丁、失效标记和重算均由 SQLite 事务原子保护。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_FILE_NOT_FOUND` | 输入文件或目录不存在 |
| `E_JSON_PARSE` | JSON/JSONL 语法错误 |
| `E_SCHEMA` | 通用 schema 或字段类型错误 |
| `E_DIR_ID_DUP` | 目录 ID 重复 |
| `E_DIR_PARENT_MISSING` | 父目录不存在 |
| `E_DIR_CYCLE` | 父链成环 |
| `E_DIR_QUOTA_INVALID` | 基础/扩展配额非法 |
| `E_EVENT_SEQ_ORDER` | 事件序号非严格递增 |
| `E_EVENT_CLOCK_ORDER` | 逻辑时钟回退 |
| `E_EVENT_UNKNOWN_DIR` | 事件引用未知目录 |
| `E_EVENT_DIGEST_FORMAT` | 摘要格式非法 |
| `E_EVENT_DIGEST_MISMATCH` | 内容摘要不一致 |
| `E_EVENT_CONTENT_LENGTH` | 内容长度与声明字节数不一致 |
| `E_RULE_SCHEMA` / `E_RULE_KEY_DUP` | 规则 schema/键重复 |
| `E_RULE_UNKNOWN_DIR` | 规则引用未知目录 |
| `E_V2_META` / `E_V2_SHARD_*` / `E_V2_KEY_DIGEST` | v2 元数据、分片或键摘要错误 |
| `E_PATCH_SCHEMA` / `E_PATCH_UNKNOWN_DIR` | 补丁非法或引用未知目录 |
| `E_PATCH_CONFLICT` | 同补丁 ID 对应不同内容 |
| `E_VERSION_ROLLBACK` | 规则包版本回退 |
| `E_SNAPSHOT_CONFLICT` | 同版本不同内容 |
| `E_RULES_NOT_MERGED` | 未建立统一规则视图即写入 |
| `E_ALREADY_LOADED` | 重复加载字典/事件流 |
| `E_DB_NOT_INITIALIZED` | 数据库尚未 load |

所有业务错误都带 `fields`，尽量保留文件、行号、序号、目录、规则键、期望值/实际值。

## 实测结果

仓库内 `artifacts/verification/summary.json` 和 `20_final_ledger_audit.json` 是
`scripts/run_demo.py --seed 42` 的实际输出：

- 种子：12 个目录、60 条写入、v1 3 条、v2 4 条、下一版 v2 5 条、替换版 v2 2 条。
- 基线：38 次成功 / 22 次失败，账本总消耗 24865，与审计扣减 24865 完全一致。
- 补丁后：34 次成功 / 26 次失败，账本 21556，保持一致；闭包包含规则子树、祖先链和真实级联目录。
- v2 追加后：33 次成功 / 27 次失败，账本 20353，保持一致。
- v2 整体替换后最终：33 次成功 / 27 次失败，账本 19805，保持一致。
- 崩溃恢复：块大小 25、在第 26 条事务内退出，重启时保留 25 条；`resume` 重放 35 条，
  最终 60 条对账一致，无重复扣减。
- 四进程各以块大小 5 并发写入，`processed` 之和为 60，最终审计 60 条且对账一致。
- 两个独立数据库重复执行相同逻辑时钟序列，`merge/status/verify` 输出逐位相同。

完整结果文件：

- `artifacts/verification/02_merge.json`
- `artifacts/verification/04_verify_before_patch.json`
- `artifacts/verification/05_patch.json`
- `artifacts/verification/06_verify_after_patch.json`
- `artifacts/verification/07_snapshot_append.json`
- `artifacts/verification/09_snapshot_rollback.json`
- `artifacts/verification/14_status_after_crash.json`
- `artifacts/verification/16_verify_after_resume.json`
- `artifacts/verification/18_verify_concurrent.json`
- `artifacts/verification/20_final_ledger_audit.json`

## 代码结构

```text
quota_diag/
  inputs.py    # 三类输入解析与业务错误码
  rules.py     # 跨版本候选合并与裁决
  engine.py    # 父链配额模拟、审计、失效闭包
  storage.py   # SQLite schema、事务与水位
  service.py   # load/merge/write/patch/snapshot/resume/status/verify
  seed.py      # 固定种子样本生成器
  cli.py       # JSON 命令行
scripts/
  gen_seed.py
  run_demo.py
tests/
  test_quota_diag.py
```

集成时可直接使用：

```python
from quota_diag import QuotaDiagnostic

diag = QuotaDiagnostic("q.db")
diag.load(dirs_path=..., writes_path=..., v1_path=..., v2_path=...)
diag.write(block_size=25)
assert diag.verify()["consistent"]
```
