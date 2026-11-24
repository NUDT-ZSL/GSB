# 敏感标记合成引擎实现说明

本项目仅使用 Python 标准库实现；`pytest` 只用于开发验证，不是运行期依赖。代码在 Python 3.10.12 与 Python 3.11.18 下均通过测试，目标运行环境为 Python 3.11+。

## 快速运行

```bash
python generate_samples.py
python -m sensitivity_engine import \
  --data data \
  --db data/ledger.sqlite3 \
  --report reports/import-report.json \
  --checkpoint checkpoints/latest.json \
  --concurrency 4 \
  --queue-limit 32 \
  --batch-timeout-ms 5000
python -m sensitivity_engine query --db data/ledger.sqlite3 --format table
python -m sensitivity_engine export-reentry \
  --db data/ledger.sqlite3 \
  --out reports/reentry-report.json
# `export` 是 `export-reentry` 的别名；`checkpoint` 是 `verify-checkpoint` 的别名。
python -m sensitivity_engine verify-checkpoint \
  --db data/ledger.sqlite3 \
  --snapshot checkpoints/latest.json
python -m sensitivity_engine verify-truth \
  --db data/ledger.sqlite3 \
  --truth truth/expected.json \
  --out reports/truth-verification.json
```

一键验证：

```bash
bash scripts/validate.sh
# 或使用 Python 3.11：
PYTHON="$(uv python find 3.11)" bash scripts/validate.sh
```

## CLI 退出码

- `0`：全部输入处理并验证成功；预期业务结论中可以包含 `E_NO_BASIS`、`E_GROUP_CYCLE` 等可复核业务码。
- `1`：存在拒绝记录、队列拒绝、预算超时、真值不一致或检查点不一致。
- `2`：命令行参数错误。Argparse 错误也归一为退出码 `2`。

## 模块结构

- `sensitivity_engine/parsing.py`：严格规范化对象、受托者、组边、来源和重入批次。
- `sensitivity_engine/hierarchy.py`：对象继承链、组图、Tarjan SCC 环检测和稳定路径展开。
- `sensitivity_engine/synthesis.py`：纯函数有效等级合成，输出全部参与来源和摘要。
- `sensitivity_engine/store.py`：SQLite schema、复合主键 `(object_key, trustee_id)` 账本、单事务批次、重入事件和检查点转储。
- `sensitivity_engine/budget.py`：`time.perf_counter()` 真实墙钟预算和有界队列。
- `sensitivity_engine/checkpointing.py`：SQLite/快照双检查点、原子替换、一致性校验和恢复重放。
- `sensitivity_engine/seed.py`：固定种子样本和显式真值。
- `sensitivity_engine/verifier.py`：依据 `grant_marker`、`path_marker`、`revocation_marker` 与组件输出比对。
- `scripts/replay_check.py`：批次排列 × 并发度的顺序无关性检查。

## 规范化与业务码

稳定业务码集中在 `sensitivity_engine/codes.py`，包括：

- 唯一性/引用：`E_DUPLICATE_OBJECT_KEY`、`E_DUPLICATE_SOURCE_KEY`、`E_PARENT_NOT_FOUND`、`E_SELF_PARENT`。
- 枚举/序号：`E_INVALID_INHERITANCE_MARK`、`E_INVALID_LEVEL`、`E_INVALID_WINDOW`、`E_SEQ_NOT_MONOTONIC`。
- 合成：`E_NO_BASIS`、`E_GROUP_CYCLE`、`E_UNDECIDABLE`、`E_OBJECT_CYCLE`。
- 预算/恢复：`E_TIMEOUT`、`E_QUEUE_FULL`、`E_LEDGER_MISMATCH`、`E_CHECKPOINT_CORRUPT`、`E_SNAPSHOT_VERSION`、`E_LAYOUT_VERSION_CHANGED`。

拒绝记录包含 `batch_id`、`op_seq`、稳定 `code`、`record_key`、来源文件和详情；同一输入中的其他记录继续处理。拒绝清单按批次、序号、业务码和规范键稳定排序。

## 合成规则

等级枚举为 `PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED`。合成器：

1. 从对象自身开始，仅在当前对象 `inheritance_mode=ALLOW` 时继续向父对象展开。
2. 收集继承链上的直接来源。
3. 将组授予沿 `parent_group -> member` 边展开到嵌套组和用户受托者。
4. 按 `(object_key, trustee_id)` 聚合；同一受托者的全部来源写入 `basis.sources`，有效等级取最高优先级，同等级按 `source_key` 稳定决胜。
5. 对象结论取全部有效受托者结论的最大值。
6. 无任何受托者依据时 `effective_level=null`、`decision_status=NO_BASIS`、`codes` 含 `E_NO_BASIS`，不会退化为最低等级。
7. 组 SCC 或自引用环中的受托者行为 `UNDECIDABLE` 并带 `E_GROUP_CYCLE`；若对象没有环外有效依据，则对象结论也为 `UNDECIDABLE`。

## 撤销、替代来源和迁移

撤销处理从事务内已持久化的 `object_ledger.basis_json` 读取撤销前依据集合，而不是使用临时展开集：

- 若账本依据中存在另一未撤销、等级 `>=` 被撤销等级的来源，则等级保持，并在 `alternative_sources` 中列出来源键、同步标记、等级和组路径。
- 否则由 SQLite 中的当前事实重新合成，等级下降或进入 `NO_BASIS`。
- 来源所在对象及其允许继承的后代均参与复评。

迁移处理先记录旧图中受托者的祖先组，再替换 `(old_parent, trustee)` 边，随后合并新图祖先组，得到旧路径和新路径可能触达的全部授予对象。所有候选对象都会复评；只有等级或可判定状态变化的对象进入 `reentry_set`/`changed_object_set`。未变化但已复评的对象保留在事件流和 `readjudication_set` 中。

## 到达顺序无关

批次稳定键为 `(logical_clock, batch_id)`；操作稳定键为 `(op_seq, canonical_key, op_type)`。导入编排使用有界队列和可配置工作线程完成规范化准备，然后按稳定键串行提交 SQLite 事务，从而避免数据库调度和线程竞争改变结论。

`scripts/replay_check.py` 对 3 个样本批次执行全部 6 种排列，并分别用并发度 1 和 4 导入：

- 总运行数：12。
- 唯一 `(ledger_digest, reentry_digest, query_digest)` 签名数：1。
- 账本摘要：`98472ae4f34efcef0558342ec73daaf7`。

## SQLite 账本和格局版本

`trustee_ledger` 使用 `PRIMARY KEY(object_key, trustee_id)`，记录有效等级、状态、依据摘要、依据 JSON、重入标记和批次水位。`object_ledger` 记录对象汇总。

当 SQLite 中 `layout_version` 或 `level_enum_version` 与输入不一致时，`Store` 会清空并重建全部账本/事实表，返回 `E_LAYOUT_VERSION_CHANGED`，不会把旧组结构结论与新结构混合。

## 资源预算和并发

- `--concurrency`：工作线程数，必须 `>=1`。
- `--queue-limit`：待处理批次队列上限；`put_nowait` 失败会生成 `E_QUEUE_FULL` 报告。
- `--batch-timeout-ms`：使用 `time.perf_counter()` 测量真实墙钟耗时；每个写入边界后检查预算。
- 超时抛出 `BudgetTimeout`，当前批次执行 `ROLLBACK`，因此已插入的来源/撤销/迁移部分写入不会留在 SQLite；`applied_batches` 不产生该批次水位。

测试 `test_wall_timeout_rolls_back_partial_batch` 使用零预算和 500 条来源，稳定观察到至少一次部分写入后超时，并验证 `sources` 与 `applied_batches` 均为 0 行。

## 检查点和强制终止恢复

检查点同时写入：

- SQLite `checkpoints` 表的 `latest` 行；
- `checkpoints/latest.json` 快照文件。

快照包含水位、账本摘要和核心表内容。文件写入先在同目录创建临时文件，写入后重新解析并通过字段校验，再 `os.replace()` 原子替换。导出的重入报告使用同样的 parse-before-rename 流程。写入新快照前会读取旧快照版本；版本不同会在结果中给出 `previous_snapshot_version` 与 `snapshot_version_changed=true`，校验时返回 `E_SNAPSHOT_VERSION`。

恢复时：

1. 比较快照文件、SQLite checkpoint 行和实时账本摘要。
2. 若实时账本有半写/损坏残留，则用最后完整检查点载荷恢复核心表。
3. 删除/忽略遗留 `.<name>.*.tmp` 半写临时文件。
4. 从检查点水位之后规范化排序并重放批次。
5. 重写检查点并再次校验。

进程被 `SIGKILL`、断电或强杀时不承诺生成退出报告；SQLite 单事务保证已回滚，文件原子替换保证快照要么是旧完整版本、要么是新完整版本。恢复只推进最后一个完整检查点之后的批次。

## 确定性样本和同步标记

`python generate_samples.py` 生成：

- `data/objects.json`、`data/trustees.json`、`data/group_edges.json`、`data/sources.json`；
- `data/batches/*.json`：新增来源、撤销、组迁移；
- `data/invalid/`：重复键、坏父引用、自引用、非法继承标记、非法等级和非单调序号；
- `truth/expected.json` 与 `data/truth.json`：显式原始授予、组路径同步标记、撤销标记和预期集合。

真值验证不只比较文本名称：每个原始授予带 `grant_marker`，每条撤销带 `revocation_marker`，每条旧/新组路径带 `path_marker`/`old_path_marker`/`new_path_marker`。验证器递归收集组件输出中的 marker 并与真值比对。

主样本最终集合：

```text
reentry_set = [
  obj:batch-add, obj:mig-gain, obj:mig-loss,
  obj:mig-rebalance, obj:revoke-down, obj:revoke-retain
]
changed_object_set = [
  obj:batch-add, obj:mig-gain, obj:mig-loss,
  obj:mig-rebalance, obj:revoke-down
]
migration_changed_set = [
  obj:mig-gain, obj:mig-loss, obj:mig-rebalance
]
```

## 实际验证记录

2026-11-25 在当前工作区执行：

- `python -m pytest -q`（Python 3.10.12，pytest 8.2.0）：`24 passed`。
- `uv run --python 3.11 --with pytest pytest -q`（CPython 3.11.18）：`24 passed`。
- `bash scripts/validate.sh`：退出码 `0`，包含生成样本、pytest、排列重放、导入、导出、检查点、真值校验和非法样本退出码 `1` 断言。
- `python scripts/replay_check.py`：`ok=true`，`permutation_runs=12`，`unique_signatures=1`。
- `verify-truth`：`ok=true`，同步标记检查全部无缺失。

## Windows 说明

实现使用 `pathlib.Path`、`tempfile.mkstemp(dir=target.parent)`、`os.replace()` 和 SQLite 跨平台文件锁，没有使用容器或 Unix-only IPC。运行身份需要对数据目录、SQLite `-journal`/WAL 辅助文件、同目录临时文件和 SQLite 锁具有创建/读写/替换权限。
