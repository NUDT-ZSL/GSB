# 本地基础设施配置差分状态演进引擎

这是一个纯本地、确定性、可集成、带 CLI 与完整样本的 Python 组件。它把 v1
单文件 JSON 配置、v2 分片 JSON + SQLite 元数据、两条独立变更流合并为带
审计能力的统一配置视图，并在注入逻辑时钟下推进状态。

运行时不访问集群、容器、账号或网络；全部输入、SQLite 文件、锁文件、事件
和测试样本都在本地。

## 快速开始

当前工作区已经准备 Python 3.11.18、SQLite 3.53.1、NumPy 2.4.6 的 `.venv`。
若在全新工作区复现：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python numpy
```

```bash
source .venv/bin/activate
python scripts/build_sample.py

python -m config_engine \
  --db state/demo.sqlite \
  --package sample/package.json \
  load

python -m config_engine \
  --db state/demo.sqlite \
  --package sample/package.json \
  merge --clock-step t0

python -m config_engine \
  --db state/demo.sqlite \
  --package sample/package.json \
  reconcile --clock-step t3

python -m config_engine \
  --db state/demo.sqlite \
  --package sample/package.json \
  status

python -m config_engine \
  --db state/demo.sqlite \
  --package sample/package.json \
  verify
```

也可以安装为命令：

```bash
uv pip install --python .venv/bin/python -e .
config-state --db state/demo.sqlite --package sample/package.json verify
```

作为 Python 组件集成：

```python
from config_engine import Engine

engine = Engine("state/demo.sqlite")
merge_report = engine.merge("sample/package.json", clock_step="t0")
event_report = engine.reconcile("sample/package.json", clock_step="t3")
status = engine.status("sample/package.json")
verification = engine.verify()
```

## 命令语义

- `load`：完整校验配置包。校验过程不写状态；只有成功后才记录本地加载审计。
- `merge`：持久化源候选和 v1 时间迁移决策，物化不可变配置版本，原子发布
  新视图快照。
- `reconcile`：按已提交事件水位处理 `manual` / `auto` 变更流，输出接受、
  否决、延期事件，然后发布演进视图。
- `resume`：扫描 `pending/running` 操作，跳过已提交步骤，只重放未完成
  步骤；不会重复计入已提交事件。
- `status`：稳定排序 JSON，包含视图、每服务状态、生效配置摘要、双流水位、
  迁移决策、恢复裁决与操作摘要。
- `verify`：重新计算最新视图并检查快照哈希、水位连续性、事件与候选关联、
  墓碑隐藏、参数来源、半提交操作等。

所有成功/业务失败输出均为 JSON。业务失败退出码为 `2`，`verify` 不一致为
`1`，故障注入主动退出为 `97`。

## 样本结果

`sample/package.json` 包含：

- v1 记录覆盖 `orders/dev`、`orders/prod`、`billing/prod`、
  `search/staging`、`legacy/prod`。
- 2 个 v2 分片与 SQLite 元数据：跨环境、两代、多代数、墓碑与墓碑后重建。
- `manual` 3 条事件、`auto` 4 条事件。
- 逻辑时钟步进：`t0=1000`、`t1=1400`、`t2=2600`、`t3=3200`、
  `t4=5000`，回拨容差 250ms。

`reconcile --clock-step t3` 后：

| 服务/环境 | 状态 | 生效版本 |
| --- | --- | --- |
| `auth/prod` | active | `2.0.0` |
| `billing/prod` | tombstoned | - |
| `cache/dev` | active | `2.0.0` |
| `legacy/prod` | tombstoned | - |
| `orders/dev` | active | `2.1.0` |
| `orders/prod` | active | `2.3.0` |
| `reports/staging` | expired | - |
| `search/staging` | tombstoned | - |

双流水位为 `manual.committed_seq=3`、`auto.committed_seq=4`。`verify`
全部检查通过。`orders/dev` 的 auto 低版本被明确否决为
`LOWER_VERSION`；`orders/prod` 的 auto `2.3.0` 在其逻辑时钟到达后在同一
代数冲突中胜出。

## 输入格式

### v1 单文件

```json
{
  "schema_version": 1,
  "logical_clock_ms": 1000,
  "records": [
    {
      "service": "orders",
      "environment": "dev",
      "replicas": 2,
      "timeout_ms": 500,
      "retry_factor": 1.5,
      "effective_after_s": 0,
      "generation": 1,
      "logical_clock_ms": 1000,
      "version": "1.0.0",
      "params": {"region": "cn-north"}
    }
  ]
}
```

`replicas`、`timeout_ms`、`retry_factor` 会进入统一参数集。若 `params`
中再次出现这些保留键，值必须与声明完全一致，否则返回
`PARAM_DECLARATION_MISMATCH`。

### v2 分片

```json
{
  "schema_version": 2,
  "shard_id": "update",
  "logical_clock_ms": 2000,
  "records": [
    {
      "service": "orders",
      "environment": "prod",
      "effective_at_ms": 2500,
      "generation": 2,
      "version": "2.0.0",
      "params": {"feature": "tiered"},
      "upstreams": ["auth"],
      "tombstone": false,
      "expires_at_ms": null
    }
  ]
}
```

SQLite 元数据要求两张表：

```sql
CREATE TABLE shard_registry (
    shard_path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE config_metadata (
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    generation INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    shard_path TEXT NOT NULL,
    record_digest TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    PRIMARY KEY (service, environment, generation, record_digest)
);
```

`sha256` 是分片原始字节哈希；`record_digest` 是规范化候选内容哈希。元数据
行必须与分片逐字段匹配。依赖服务必须在同一环境存在，依赖图按每个
service/environment 的最新 v2 记录检测环。

### 变更事件

```json
{
  "schema_version": 1,
  "source": "manual",
  "events": [
    {
      "event_id": "manual-orders-dev-2.1.0",
      "seq": 1,
      "logical_clock_ms": 1200,
      "service": "orders",
      "environment": "dev",
      "target_version": "2.1.0",
      "generation": 3,
      "effective_at_ms": 2200,
      "content_digest": "<sha256>",
      "params": {"feature": "tiered"},
      "upstreams": [],
      "tombstone": false,
      "expires_at_ms": null
    }
  ]
}
```

`content_digest` 覆盖 service、environment、generation、target_version、
params、upstreams、tombstone、expires_at_ms。事件也可省略内容并通过
`target_version + content_digest` 引用包内已有候选。

## 确定性裁决

### 跨格式提案

候选提案排序键（大者优先）：

```text
(logical_clock_ms, generation, source_format_priority,
 effective_at_ms, version, content_digest)
```

`v2` 格式优先级为 20，`v1` 为 10。所有源候选都保留为时间线；若高代 v2
尚未生效，低代或同代 v1 可以继续作为当前生效配置，避免未来配置提前影响
视图。`arbitration` 报告独立给出“最新提案”及首个落选理由。

### 事件冲突

事件首先检查代数不回退，再按以下顺序裁决：

```text
generation >= head
version desc
source_priority desc (manual=20, auto=10)
effective_at desc
event_id/content_digest deterministic tie-break
```

同代冲突的落选事件在最终审计中统一标为 `rejected`，并给出首个理由，例如
`LOWER_VERSION`、`LOWER_GENERATION`、`LOWER_SOURCE_PRIORITY`、
`EARLIER_EFFECTIVE_AT`。这样即使双源并行、到达顺序不同，最终审计状态也
一致。

### 状态边界

- `pending`：当前没有已生效版本，最早版本尚未到达 `effective_at_ms`。
- `active`：最高已生效代数不墓碑且未过期。
- `expired`：最高已生效代数满足 `expires_at_ms <= clock_ms`。
- `tombstoned`：最高已生效代数为墓碑；不会进入 `effective_services`，
  但仍保留在 `history`、`tombstones` 和版本表中。

边界均使用闭区间：`effective_at_ms <= clock_ms` 生效，
`expires_at_ms <= clock_ms` 失效。

## v1 时间迁移

首次 merge 时：

```text
effective_at_ms =
    injected_clock_ms + relative_effective_after_s * 1000
```

迁移决策持久化以下字段：

- `source_relative_seconds`：换算前原始值。
- `scale_ms_per_second=1000`。
- `rounding_direction=toward_positive_infinity`（整数秒乘法无实际误差）。
- `migration_clock_ms`、`effective_at_ms`、原始时间表示。

同一候选后续 merge 复用首次决策。请求时钟落后已持久化水位超过
`rollback_tolerance_ms` 时返回 `CLOCK_ROLLBACK`，不写视图。容差内请求使用
已持久化水位，不回拨业务时间。

## 事务、并发与恢复

实现分两层：

1. 不可变事实提交：每个事件提交在单个 `BEGIN IMMEDIATE` 事务内同时写
   `event_commits`、流水位、事件候选和审计；每个合并步骤以幂等键写源候选、
   迁移决策或配置版本。
2. 原子发布：最终 `commit_view` 在文件发布锁 + SQLite 写事务内重新读取
   全部已提交事实，重建视图，写快照、逻辑时钟水位、状态迁移和操作完成
   标记，并把双流水位、迁移决策、状态迁移与时钟固化到
   `published_recovery_snapshots` 恢复包。读取方只选择已提交快照。

每个命令有确定性 `op_id` 与有序 `operation_steps`。进程在步骤提交后被
`SIGKILL` 时，操作系统自动释放 `fcntl` 锁，SQLite 回滚未提交事务；重启后
`resume` 跳过 `committed` 步骤，只执行剩余步骤。事件序号与候选唯一键保证
已提交事件不会重复计入。

并发约束：

- 同一事件流使用流级文件锁，保证序号推进串行。
- 同一操作使用操作级文件锁，保证只有一个实例执行。
- SQLite `BEGIN IMMEDIATE` + WAL + `busy_timeout` 串行化物理写事务。
- 不同事件流可并行推进，最终发布锁保证最新快照包含事务可见的全部事实。
- 唯一键与代数/版本检查防止服务代数和事件序号被并发回退。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `CONFIG_PACKAGE_MISSING` | 包、分片、SQLite 元数据或事件文件缺失 |
| `JSON_MALFORMED` | 非法/重复键/非严格 JSON |
| `DUPLICATE_SERVICE_ENV` | v1 服务环境组合重复 |
| `INVALID_REPLICAS` | 副本数不是正整数 |
| `INVALID_TIMEOUT` | 超时不是非负整数 |
| `INVALID_RETRY_FACTOR` | 重试倍率不是有限正数 |
| `INVALID_EFFECTIVE_AFTER` | 相对生效秒数不是非负整数 |
| `INVALID_PARAMS` | 参数映射非扁平 JSON 标量表 |
| `PARAM_DECLARATION_MISMATCH` | 保留参数与 v1 声明冲突 |
| `SHARD_HASH_MISMATCH` | v2 分片哈希不匹配 |
| `GENERATION_REGRESSION` | 代数回退 |
| `GENERATION_CONFLICT` | 同代不可变内容冲突 |
| `INVALID_EFFECTIVE_AT` | 绝对生效时间非整数毫秒 |
| `MISSING_UPSTREAM` | 依赖上游不存在 |
| `DEPENDENCY_CYCLE` | 依赖图成环 |
| `CLOCK_ROLLBACK` | 逻辑时钟回拨超过容差 |
| `EVENT_TARGET_UNKNOWN` | 事件引用未声明服务环境 |
| `EVENT_SEQUENCE_INVALID` / `EVENT_SEQUENCE_GAP` / `EVENT_SEQ_CONFLICT` | 事件序号结构冲突 |
| `CONTENT_DIGEST_MISMATCH` | 内容摘要不匹配 |
| `EVENT_CONTENT_NOT_FOUND` | 事件引用内容不存在 |
| `LOWER_GENERATION` / `LOWER_VERSION` / `LOWER_SOURCE_PRIORITY` / `EARLIER_EFFECTIVE_AT` | 冲突落选首个理由 |
| `BASE_CONFIG_MISSING` | 未 merge 就开始 reconcile |
| `VERIFY_FAILED` | 持久化状态与可见视图不一致 |

每个错误 JSON 都保留 `code`、`message` 与确定性的 `details` 诊断字段。

## 故障注入

测试通过环境变量在指定步骤提交后调用 `os._exit(97)`，模拟无法捕获的强制
终止：

```bash
CONFIG_ENGINE_CRASH_AFTER_STEP=merge:record_migrations \
python -m config_engine --db state/crash.sqlite --package sample/package.json \
  merge --clock-step t0

python -m config_engine --db state/crash.sqlite --package sample/package.json resume
```

可注入点包括 `merge:persist_source_candidates`、`merge:record_migrations`、
`merge:materialize_versions`、`reconcile:event:<source>:<seq>`、
`reconcile:commit_view`。

## 验证

```bash
.venv/bin/python -m unittest discover -v
pyflakes config_engine tests scripts
.venv/bin/python scripts/run_acceptance.py
```

当前实测结果见 `docs/VERIFICATION.md`。固定样本的重复运行会产生逐位一致的
`merge`、`reconcile`、`status`、`verify` JSON；强杀恢复后的视图、双流水位
与迁移决策和不中断运行一致。

## 目录

```text
config_engine/      可集成引擎与 CLI
sample/             固定本地样本、SQLite 元数据、期望状态
scripts/            样本构建与验收脚本
tests/              标准库 unittest
docs/               设计与实测说明
```
