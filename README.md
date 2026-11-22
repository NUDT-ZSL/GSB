# Warbiter

Warbiter 是一个从零实现的多租户写写冲突仲裁器。它不尝试实现通用 MVCC
可见性判定，而是把每一次提交、冲突、断言重估、策略选择、裁决和最终值来源
写成可复现、可追溯的 SQLite 账本。

实现只使用 Python 标准库（`sqlite3`、`json`、`hashlib`、`argparse` 等），
不访问网络，不使用容器或外部服务。

## 快速开始

本工作区已确认可用环境：

- Python `3.12.13`（要求 `>=3.11`）
- SQLite `3.53.1`
- `python -c "import sqlite3"` 通过
- 本地文件系统 `os.replace` 原子重命名通过

本机默认 `python3` 是 3.10；下面的命令使用已安装的 3.12 解释器。也可以把
`PY` 替换成任意 Python 3.11+ 解释器。

```bash
PY=/home/zsl/.local/bin/python

$PY scripts/generate_samples.py

$PY -m warbiter init \
  --db /tmp/warbiter.db \
  --schema samples/schema.json \
  --config samples/arbiter.json

$PY -m warbiter apply \
  --db /tmp/warbiter.db \
  --script samples/transactions.json

$PY -m warbiter conflict --db /tmp/warbiter.db
$PY -m warbiter resolve  --db /tmp/warbiter.db
$PY -m warbiter status   --db /tmp/warbiter.db
$PY -m warbiter verify   --db /tmp/warbiter.db
```

若要一条命令完成提交和裁决：

```bash
$PY -m warbiter apply --db /tmp/warbiter.db \
  --schema samples/schema.json --config samples/arbiter.json \
  --script samples/transactions.json --resolve
```

数据库不存在时，`apply` 在同时提供 `--schema` 和 `--config` 的情况下会自动初始化。

## 输入协议

所有样本由 `scripts/generate_samples.py` 确定性生成，并写入
`samples/manifest.json` 的 SHA-256 清单。

### 租户与表：`samples/schema.json`

```json
{
  "format": "warbiter-schema-v1",
  "tenants": [
    {
      "name": "acme",
      "tables": [
        {
          "name": "inventory",
          "primary_key": "sku",
          "columns": [
            {"name": "sku", "type": "string", "nullable": false},
            {"name": "qty", "type": "integer", "nullable": true}
          ],
          "conflict_policy": {
            "version_label": "inventory-v1",
            "row_strategy": "last_write_wins",
            "row_overwrite_columns": ["price", "status"],
            "column_strategies": {
              "qty": "column_merge",
              "note": "column_merge",
              "owner": "last_write_wins"
            },
            "same_column_merge": "last_write_wins",
            "allowed_strategies": [
              "last_write_wins",
              "column_merge",
              "reject_rollback",
              "manual_review"
            ]
          }
        }
      ]
    }
  ]
}
```

校验规则：

- 租户名唯一；同租户内表名唯一；同表内列名唯一。
- 列类型闭合于 `string`、`integer`、`number`、`boolean`。
- 策略标签闭合于 `last_write_wins`、`column_merge`、
  `reject_rollback`、`manual_review`。
- `row_strategy` 和 `same_column_merge` 不允许是 `column_merge`。
- `row_overwrite_columns` 与 `column_strategies` 中同列不能同时声明整行覆盖和
  逐列合并；运行期也对合并列采用逐列策略优先，避免整行写入静默覆盖。
- 主键必须存在且非空。

### 事务脚本：`samples/transactions.json`

```json
{
  "format": "warbiter-script-v1",
  "transactions": [
    {
      "tx_id": 1003,
      "tenant": "acme",
      "start_clock_ms": 1004,
      "isolation": "serializable",
      "writes": [
        {
          "op": "conditional_update",
          "table": "inventory",
          "row_key": "SKU-1",
          "values": {"status": "hold"},
          "assertion": {"column": "qty", "op": "ge", "value": 10},
          "recorded_observation": {"qty": 12, "status": "available"}
        }
      ]
    }
  ]
}
```

支持的操作：

- `insert`：必须包含主键，主键值必须等于 `row_key`。
- `update`：只写声明的列；可设置 `"whole_row": true`。
- `delete`：整行删除。
- `conditional_update`：更新列并在裁决时重估 JSON 断言。

断言是闭合的 JSON AST，不执行 Python 或 SQL：

- 叶子：`{"column","op","value"}`，其中 `op` 为
  `eq/ne/lt/le/gt/ge/in/is_null/not_null`。
- 组合：`{"all":[...]}`、`{"any":[...]}`、`{"not":...}`。

`recorded_observation` 只作为录制观察留档；裁决绝不使用它。

### 仲裁配置：`samples/arbiter.json`

```json
{
  "format": "warbiter-config-v1",
  "conflict_tolerance_ms": 5,
  "logical_clock_step_ms": 10,
  "max_conflict_replay": 3,
  "ledger_retention_generations": 8,
  "assertion_failure_action": "rollback"
}
```

`assertion_failure_action` 可取 `rollback` 或 `manual_review`。

## 裁决语义

### 提交顺序与冲突窗口

- 事务提交逻辑时钟为 `start_clock_ms + logical_clock_step_ms`。
- 全局顺序键固定为 `(commit_clock_ms, tx_id)`，并在 `commit_log` 中重建为
  稠密有序的提交序列表。
- 同一 `(tenant, table, row_key)` 上的每对事务按提交顺序检查。
- 后提交事务满足
  `right.start_clock_ms <= left.commit_clock_ms + conflict_tolerance_ms`
  时视为写区间重叠。
- 写集合：`insert/delete/whole_row update` 覆盖全部列；普通 `update` 覆盖显式列。

冲突分类：

- `true`：时间窗口重叠且写列集合相交。
- `pseudo`：时间窗口重叠但写列集合不相交，可安全并存。
- `serialized`：时间窗口不重叠，不按并发冲突裁决。

三类都会进入证据账本，但只有 `true` 会触发拒绝或挂起。

### 策略选择

对冲突中后提交事务的每个争用列：

1. `delete` 一律使用表级 `row_strategy`。
2. 列在 `column_strategies` 中时使用列策略。
3. 否则使用表级 `row_strategy`。
4. `column_merge` 遇到同列冲突时使用 `same_column_merge` 作为显式回退，
   不会静默覆盖。

因此同一张表可以同时有整行覆盖策略和逐列合并策略。样本中
`tx 1008` 对 `SKU-3` 做整行插入，与 `tx 1006` 的冲突证据同时显示：

- `qty`、`note` 使用 `column_merge`；
- `price`、`status` 使用 `last_write_wins`。

### 事务终态

- `accepted`：写入生效。
- `rejected_conflict`：`reject_rollback`，后提交事务全部写入撤销。
- `suspended_manual`：`manual_review`，行被挂起，写入不生效。
- `aborted_assertion`：条件更新断言在裁决时失败并回滚。
- `suspended_assertion`：断言失败转人工，行挂起。
- `suspended_row`：行此前已挂起，后续写入被保护性挂起。
- `suspended_replay_limit`：超过 `max_conflict_replay`。

若同一事务同时命中 `manual_review` 与 `reject_rollback`，固定采用更保守的
`manual_review` 优先。

### 条件更新

条件更新在事务被裁决前，基于当时已接受事务物化出的记录版本重新求值。证据包含：

- 断言原文（规范 JSON）；
- 录制时观察；
- 重估所依据的记录版本与完整记录值；
- 求值结果；
- 回滚或人工动作。

样本 `tx 1003` 录制时看到 `qty=12`，但 `tx 1002` 先提交使 `qty=8`，
裁决时断言 `qty >= 10` 为假，因此整个事务回滚。样本 `tx 1204` 的断言在同一
机制下为真并提交。

删除操作由样本 `tx 1205 -> tx 1206` 覆盖：`tx 1205` 插入 `metrics#2`，
`tx 1206` 并发删除同一行，按行级 `last_write_wins` 接受删除；最终行状态保留
`deleted=true`、`version=2` 和删除事务号，便于验账。

## 持久化与版本

SQLite 状态表：

- `tx`、`commit_log`、`tx_writes`、`tx_audit`、`tx_assertions`
- `conflicts`、`assertion_evidence`、`effective_writes`
- `row_state`、`suspensions`
- `policy_versions`、`ledger_versions`、`engine_state`

关键性质：

- `apply` 将整份脚本的校验后事务、写操作、审计和冲突检测放在一个
  `BEGIN IMMEDIATE` 事务内。
- `resolve` 将行状态、挂起标记、断言证据、冲突证据、有效写溯源、账本快照、
  水位和摘要放在一个事务内。
- 每笔冲突在检出时绑定 `policy_version`。之后策略升级不会改变该冲突的裁决。
- 新策略通过临时表 `policy_stage` 一次性插入 `policy_versions`，再单点更新
  `current_policy_version` 指针；读者只能看到完整旧版本或完整新版本。
- 每次产生新裁决都会生成完整 `ledger_versions` 快照和 SHA-256 摘要，并按
  `ledger_retention_generations` 删除过旧快照。
- 同一 `tx_id` 只允许一个 payload；完全相同 payload 的重提交幂等跳过，
  不同 payload 返回 `E_TX_CONFLICT`。

## 故障恢复

`apply` 和 `resolve` 都是单 SQLite 事务。进程在事务中途被 `SIGKILL`/`_exit`
终止时，SQLite 回滚整笔事务，不会留下半提交事务或半裁决账本。

`resume` 会：

1. 将遗留的 `resolving` 冲突复位为 `pending`；
2. 重建提交序列表；
3. 只处理仍为 `committed` 的事务和 `pending` 冲突；
4. 重放既有 `accepted` 事务以重建行状态；
5. 生成新账本并校验。

已裁决事务不会再次回滚或合并。

测试入口通过 `WARBITER_FAULT_APPLY=N` 和 `WARBITER_FAULT_RESOLVE=N`
在事务中原生退出，验证恢复后与无中断运行逐位一致。

## 并发与幂等

- 所有写命令使用 SQLite `BEGIN IMMEDIATE`、WAL、`busy_timeout=60000` 和
  `synchronous=FULL` 串行化写事务。
- 多个进程并行提交不同事务时，唯一 `tx_id`、唯一冲突键
  `(left_tx, right_tx, tenant, table, row_key)` 和单事务裁决避免重复写入。
- 重复提交同一事务返回 `skipped_idempotent`，不会重复入账。
- 同一行同一列的最终值只在单事务的确定性重放中合并，不会并发覆盖中间状态。

## 子命令

- `init`：初始化 DB，或显式装载策略版本。
- `policy`：物理 schema 不变时声明新策略版本，不改写已有行。
- `apply`：校验、暂存、提交并检测冲突；`--resolve` 可立即裁决。
- `conflict`：输出全部冲突证据，含待裁决或已裁决状态。
- `resolve`：按确定性顺序裁决所有待处理冲突。
- `resume`：崩溃恢复并完成未裁决工作。
- `status`：稳定排序 JSON，含行级挂起标记、冲突统计、账本摘要。
- `verify`：独立校验。

业务错误示例：

- `E_BAD_SCHEMA`、`E_BAD_POLICY`、`E_BAD_SCRIPT`、`E_BAD_CONFIG`
- `E_TX_CONFLICT`、`E_COMMIT_ORDER`、`E_POLICY_CHANGED`、`E_REPLAY_LIMIT`
- `E_NOT_INITIALIZED`、`E_SCHEMA_CHANGED`
- `E_VERIFY_FAILED`

非法输入在打开/写入持久化状态前被拒绝；CLI 测试确认数据库文件不会创建。

## verify 检查

`verify` 顺序重放已接受事务并检查：

- `commit_log` 与 `(commit_clock_ms, tx_id)` 顺序一致；
- 冲突候选键、类型与列集合可从事务写集重新推导；
- 每条证据 JSON 的 SHA-256 与列中摘要一致；
- 所有冲突已裁决；
- 断言可按记录版本重新求值；
- `row_state` 与全量 accepted-write 重放逐字段一致；
- `effective_writes` 的每个最终写来源都有已接受事务；
- 当前冲突账本摘要与 `ledger_versions` 指针一致；
- 待裁决冲突集合摘要一致。

## 实测结果

执行：

```bash
/home/zsl/.local/bin/python scripts/run_validation.py
```

最近一次结果（完整数据见 `artifacts/validation_report.json`）：

- 总结果：PASS
- Python `3.12.13`，SQLite `3.53.1`
- 主样本：20 条冲突（`true=14`、`pseudo=6`），全部裁决
- 事务终态：`accepted=15`、`rejected_conflict=1`、
  `suspended_manual=1`、`aborted_assertion=1`
- 裁决分布：`later_accepted=11`、`later_rejected=1`、
  `later_suspended_manual=1`、`assertion_aborted=1`、`safe_merge=6`
- 两次独立运行：状态 JSON 完全一致，账本和最终记录集摘要一致
- 中断 `resolve` 后恢复：20 条冲突重新裁决后与无中断运行一致
- 中断 `apply`：事务数 0、冲突数 0，重跑后与无中断运行一致
- 4 个进程并行提交：全部退出 0，最终行 `{"id":1,"value":4}`，
  `verify` 通过
- 策略 v2 生效后：待裁决冲突仍使用 v1，`tx 1102` 仍按
  `reject_rollback` 回滚
- 单元测试：13 个用例全部通过

报告文件：

- `artifacts/validation_report.md`
- `artifacts/validation_report.json`
- `artifacts/demo_conflicts.json`
- `artifacts/demo_status.json`
- `artifacts/demo_verify.json`

## 测试与文件

```bash
/home/zsl/.local/bin/python -m unittest discover -s tests -v
```

主要文件：

- `warbiter/model.py`：输入校验、规范化、断言 AST
- `warbiter/storage.py`：SQLite schema、策略版本临时表替换
- `warbiter/engine.py`：提交、冲突检测、裁决、恢复、verify
- `warbiter/cli.py`：命令入口
- `scripts/generate_samples.py`：确定性样本生成
- `scripts/run_validation.py`：验收与报告生成
- `tests/test_warbiter.py`：13 个集成/回归用例

## 设计边界

- 本应用刻意实现写写冲突仲裁，不实现完整 MVCC 快照可见性。
- 人工挂起被持久化，但未提供“人工恢复/释放行”子命令；挂起行会保护性阻断后续写入。
- 冲突检测对同一行事务做成对检查，适合本任务的可追溯性要求；超大规模行对场景需要分片。
- 账本保留代数当前作用于全量 `ledger_versions` 快照；`conflicts` 证据保持完整以支持验账。
