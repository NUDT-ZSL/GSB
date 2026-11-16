# 血缘迁移验证工具 — 技术说明

## 1. 架构与模块

| 模块 | 职责 |
| --- | --- |
| `lineage_migrator/models.py` | 字段、血缘边、迁移规则、样本加载的不可变数据模型 |
| `lineage_migrator/graph.py` | 内存图状态、确定性环检测（DFS 三色标记）、权重分桶 |
| `lineage_migrator/rules.py` | 五类规则语义：rename / split / merge / type_change / deprecate |
| `lineage_migrator/engine.py` | 版本递增校验、规则排序冲突检测、逐规则执行、写库前环检测、单事务提交 |
| `lineage_migrator/store.py` | SQLite 建表、快照、版本表、变更日志、审计事件、回滚与 SHA-256 指纹 |
| `lineage_migrator/diff.py` | 仅基于快照表的血缘差异报告与规则归因下钻 |
| `lineage_migrator/injector.py` | 异常配置加载、隔离库注入、回滚一致性断言、双跑比对 |
| `lineage_migrator/api.py` | FastAPI 控制台后端 |
| `lineage_migrator/console.py` | 离线交互式 REPL 控制台 |
| `scripts/run_demo.py` | 无网络端到端演示，结果写入 `artifacts/demo_result.json` |

## 2. SQLite 持久化模型

- `meta`：单行，记录是否初始化与最后已提交版本；进程重启后只认已提交版本。
- `field` / `edge`：当前存活状态，每个版本提交时在同一事务内整体替换。
- `snapshot_field` / `snapshot_edge`：每个版本不可变快照，差异报告只读取这两张表。
- `version`：版本号、状态、计数与指纹；基线固定为版本 0。
- `change_log`：每条图变更及其规则、版本、动作，用于差异归因。
- `audit_event`：字段废弃时被删除的入边/出边清单等审计事实。

每个版本的全部写入包裹在一个 SQLite 事务中，任何异常都 `ROLLBACK`，数据库回到该版本应用前的状态，历史版本不受影响。失败尝试不写任何版本行或变更日志。

## 3. 五类规则语义

- **rename**：改写所有引用该字段的边端点；目标字段已存在或目标缺失分别报 `E_RULE_ORDER` / `E_TARGET_MISSING`。
- **split**：入边复制到每个拆分目标；出边按权重经 `blake2b(edge_id) mod 总权重` 确定性分桶；源字段移除，新边 `origin` 记录 `split:规则:父边`。
- **merge**：汇聚源字段的边到目标字段，按 `(source,target)` 去重，`origin` 记录 `merge:规则:父边` 保留来源标识；会产生自环的边保留并交由环检测拦截。
- **type_change**：仅更新字段类型，边集合完全不变。
- **deprecate**：删除该字段全部入边与出边，字段标记废弃，删除清单写入 `audit_event`。

## 4. 顺序冲突与业务码

同一版本内：

- 全部规则带唯一 `seq`：按 `(seq, rule_id)` 排序；
- 或使用 `depends_on`：确定性拓扑排序，依赖环/未知依赖报 `E_RULE_ORDER`；
- 两种机制混用、`seq` 重复、或多规则触碰同一字段却无任何排序依据：`E_RULE_ORDER` 且整版本回滚。

业务码：`E_RULE_ORDER`、`E_CYCLE`、`E_TARGET_MISSING`、`E_STALE_SOURCE`、`E_VERSION_REGRESS`，另有兜底 `E_INTERNAL`。环检测、缺失与废弃源检测都发生在事务提交前；版本号必须严格等于 `last_version + 1`，否则报 `E_VERSION_REGRESS`。

## 5. 可复现性

- 所有排序使用显式键（字段引用、边 ID、规则 ID），不依赖字典插入顺序或内存布局。
- 权重分桶使用稳定哈希；指纹为规范化字段/边集合的 SHA-256。
- 演示脚本从空库独立重放两次，比对版本序列、指纹与最终边集合逐位一致。
- 异常场景各自使用独立 SQLite 文件，注入前后比对指纹与边数，断言 `rolled_back_cleanly`。

## 6. 运行方式

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows；POSIX 为 source .venv/bin/activate
pip install -r requirements.txt
python scripts/run_demo.py    # 端到端演示
pytest                        # 测试套件
uvicorn lineage_migrator.api:app --reload
python -m lineage_migrator.console
```

SQLite 使用 Python 标准库内置版本，无需额外服务；全程不访问网络。
