# 资源编排依赖图谱跨版本诊断组件

本工程在 Linux/WSL Ubuntu、Python 3.10+ 上交付一个仅依赖标准库的本地组件，用于：

- 校验服务模板、实例化请求、v1 单文件策略与 v2 分片+SQLite 元数据策略。
- 跨 v1/v2 合并统一依赖视图，并持久化每次裁决、边来源和 v1 时间换算。
- 沿依赖边计算传递闭包、配置指纹与分支内失效传播。
- 在实例化时裁决拓扑闭合、版本兼容、节点资源预算与模板并发上限。
- 以轮次预算调度请求，原子扣减资源账本并登记实例。
- 以版本化影子视图原子切换策略，进行中请求绑定开始时的策略版本。
- 在进程强制终止后识别未完成拓扑、半提交账本与未完成策略作业，只重放缺失部分。
- 通过 SQLite 事务/约束和 `flock` 文件锁支持多进程并保持幂等。

详细事务边界、数据模型与恢复算法见 [docs/DESIGN.md](docs/DESIGN.md)。

## 环境

```bash
python3 --version   # >= 3.10
sqlite3 --version   # 仅用于人工检查，Python 使用标准库 sqlite3
```

工程不访问网络，不使用 Docker，不依赖第三方 Python 包。

## 快速开始

```bash
python3 -m topodiag load     --workdir ./topodiag-data
python3 -m topodiag build    --workdir ./topodiag-data
python3 -m topodiag schedule --workdir ./topodiag-data --round-budget-ms 250 --workers 4
python3 -m topodiag status   --workdir ./topodiag-data
python3 -m topodiag verify   --workdir ./topodiag-data
```

也可以执行 `./bin/topodiag <子命令>`。所有成功和失败结果均输出 JSON；日志追加到
`<workdir>/logs/topodiag.log`。

增量策略切换：

```bash
python3 -m topodiag build \
  --workdir ./topodiag-data \
  --policy-package ./topodiag-data/samples/policies/v2-update
```

强制终止与恢复验证：

```bash
# 以下退出码由故障注入产生：78=策略提交前退出，79=调度中途退出
python3 -m topodiag schedule --workdir ./topodiag-data \
  --round-budget-ms 250 --crash-at resource

python3 -m topodiag recover --workdir ./topodiag-data --round-budget-ms 250
python3 -m topodiag verify  --workdir ./topodiag-data
```

## 子命令

| 子命令 | 作用 |
| --- | --- |
| `load` | 用固定种子生成模板、请求、拓扑容量、v1/v2 策略及 v2 增量包 |
| `build` | 校验输入、解析并合并策略、建立闭包/失效事件、原子激活策略版本；可接收新 v2 包 |
| `schedule` | 按轮次预算和目标命名空间调度，执行拓扑、并发和资源裁决 |
| `recover` | 清理 staging 版本、修复账本/并发计数、重放未完成拓扑和待处理策略作业 |
| `status` | 输出稳定排序 JSON：策略指针、闭包摘要、资源余量、水位、实例和请求结局 |
| `verify` | 校验 SQLite 完整性、闭包指纹、依赖摘要、账本守恒、并发计数、实例一致性等 |

常用选项：

- `load --seed N --force`：只重新生成 `samples/`，不删除运行时数据库。
- `build --clock MS`：显式指定注入逻辑时钟；低于持久化水位超过 100 ms 时返回 `CLOCK_ROLLBACK`。
- `build --policy-package DIR`：应用 v2 完整替换包或增量包。
- `schedule --round-budget-ms N --workers K`：每轮逻辑时间预算与命名空间预检线程数。
- `--crash-at`：仅用于验收的事务内故障注入。

## 确定性样本

默认种子为 `20261126`，生成到 `<workdir>/samples/`：

```text
samples/
├── templates.json          # 10 个服务模板与 DAG
├── requests.json           # 18 个请求，覆盖成功/拒绝/失败/放弃/预算耗尽
├── topology.json           # 3 个节点、4 个命名空间、资源与模板并发上限
├── input_manifest.json
├── expected_scenarios.json
└── policies/
    ├── v1.json             # 单文件、相对生效秒数、无拓扑约束声明
    ├── v2/                 # generation 2，分片 JSON + metadata.sqlite
    └── v2-update/          # generation 3，增量包
```

样本刻意包含以下可验证场景：

- `archive` 只在 v1 出现：用于验证相对秒换算和 v1 边来源。
- `catalog` 在 v1/v2 中逻辑时钟与代数相同：用于验证 v2 来源优先级裁决。
- v2 generation 2 将 `legacy` 标记为依赖闭包墓碑：请求 9 出现首个拓扑断点。
- generation 3 更新 `analytics` 标签/版本：只重算 `analytics` 与下游 `report`。
- generation 3 墓碑化 `retired` 并使 `namespace:ns-red` 拓扑失效：请求 10、11 被拒绝。
- 请求 3/17 触发模板并发上限，请求 16 触发节点资源不足。
- 请求 14 启动后失败，请求 15 主动放弃，请求 18 因轮次预算耗尽未启动。

## 核心语义

### 跨版本裁决

同一模板同时出现在 v1/v2 时，按以下字典序选择：

```text
(logical_clock, policy_generation, source_rank)
source_rank: v2 = 1, v1 = 0
```

因此：

- 逻辑时钟更大者胜。
- 时钟相同则策略代数更大者胜。
- 时钟与代数均相同则 v2 优先。

v1 边的原始时间与换算信息会持久化：

```text
original_time = {kind: relative_seconds, value: 5, snapshot_logical_clock_ms: 12000}
effective_at_ms = injected_clock_ms + value * 1000
conversion = {
  scale_ms_per_second: 1000,
  rounding: exact_integer,
  rounding_direction: none,
  pre_round_value: 5
}
```

每条 `policy_edges` 还保存来源格式、来源策略版本、逻辑时钟、策略代数、裁决依据和生效时刻。
`merge_decisions` 以 `(policy_key, logical_clock, template_name)` 持久化裁决 JSON 和哈希；
相同时钟重复判定发生字节级变化时返回 `MERGE_DECISION_DRIFT`。

### 闭包与失效

- 闭包只包含传递上游模板；请求拓扑为“根模板 + 闭包”。
- `closure_digest` 对闭包成员稳定排序后计算。
- `config_fingerprint` 覆盖闭包内每个成员的版本、标签、依赖、墓碑、来源和生效时刻。
- 模板版本、标签、依赖、墓碑或来源变化时，只重算该模板及闭包包含它的下游模板。
- 其他分支的闭包、指纹和 `revision` 原样复制，必须逐字节一致。
- 每个失效事件保存触发模板/版本/代数/逻辑时钟、受影响下游、完整传播链及前后修订号。

### 实例化裁决

首个断点按稳定顺序选择：

1. 命名空间不存在或 `namespace:<name>` 被 v2 标记失效。
2. 拓扑深度超过命名空间上限。
3. 依赖优先序中出现缺失模板、墓碑模板、`template:<name>` 失效或版本冲突。

版本兼容规则为：策略节点版本必须 `>=` 服务模板清单版本。

资源裁决先检查目标模板在该节点的并发上限，再按资源名稳定排序检查预算。
决策 JSON 保存每个资源项的 `capacity`、`used_before`、`available_before`、`required`
以及首个触发约束。成功提交时 `ledger_events` 再记录实际扣减前后的数值。

### 调度结局

严格区分：

| outcome | 含义 |
| --- | --- |
| `completed` | 拓扑与资源均满足，实例、资源扣减和请求状态同事务提交 |
| `rejected` | 预算内未启动，因拓扑、版本、并发或资源约束被拒绝 |
| `started_failed` | 已开始后实例启动失败，不扣减资源 |
| `abandoned` | 请求主动放弃 |
| `not_started` | 本轮逻辑时间预算耗尽，请求未启动 |

每个请求对同一策略版本至多提交一次；实例 ID 唯一。已终态请求重复调度不会重复扣减或注册。

### 策略切换原子性

- 先校验清单语义版本、代数下限、分片字节哈希、依赖摘要和基础代数。
- 新版本写入 `state='staging'` 的影子版本行，不修改当前活动版本。
- 节点、边、闭包、墓碑、失效事件全部写完后，同一事务将版本改为 `committed` 并整体切换
  `policy_pointer`。
- 进行中请求将 `policy_version_id` 固定为开始时的版本；旧版本行永久保留供其完成。
- 旧包返回 `POLICY_ROLLBACK` 或 `POLICY_GENERATION_ROLLBACK`；同代不同摘要返回
  `POLICY_GENERATION_CONFLICT`；同包重复提交为 `noop`。

### 恢复

`recover` 在一个事务中完成：

- 删除未提交 staging 版本；活动指针不变。
- 将遗留 `processing` 请求释放为 `recovery_pending`。
- 依据已提交 `instance_resources` 重建资源账本和模板并发计数。
- 检查 `ledger_events` 链，必要时按 `committed_seq` 重建。
- 协调“实例已提交但请求未完成”和“请求完成但实例缺失”的异常。
- 重建已提交实例水位。
- 重放 `policy_apply_jobs` 中 `pending` 的策略包。
- 若首次策略构建在指针提交前退出，自动重建初始策略。
- 仅调度未完成请求，不重试已终态请求。

## 错误 JSON

业务错误统一返回：

```json
{
  "ok": false,
  "error": {
    "code": "SHARD_HASH_MISMATCH",
    "message": "v2 shard 1 hash does not match SQLite metadata",
    "diagnostics": {
      "template_name": null,
      "version": "2.0.0",
      "shard_index": 1,
      "request_seq": null,
      "expected_sha256": "...",
      "actual_sha256": "..."
    }
  }
}
```

所有业务错误固定包含 `template_name`、`version`、`shard_index`、`request_seq` 四个诊断键。
主要独立错误码包括：

- 输入/依赖：`MISSING_DEPENDENCY`、`DEPENDENCY_CYCLE`、`TEMPLATE_NAME_DUPLICATE`、
  `REQUEST_SEQ_DUPLICATE`、`INVALID_RESOURCE_REQUIREMENT`、`INVALID_LABEL`。
- 时间：`INVALID_V1_RELATIVE_TIME`、`INVALID_LOGICAL_CLOCK`、`CLOCK_ROLLBACK`。
- v2：`SHARD_HASH_MISMATCH`、`V2_SHARD_INDEX_MISMATCH`、`V2_METADATA_MISMATCH`、
  `POLICY_DEPENDENCY_DIGEST_MISMATCH`。
- 策略版本：`POLICY_ROLLBACK`、`POLICY_GENERATION_ROLLBACK`、
  `POLICY_GENERATION_CONFLICT`、`POLICY_BASE_GENERATION_MISMATCH`。
- 调度：`TOPOLOGY_BREAK`、`RESOURCE_INSUFFICIENT`、
  `TEMPLATE_CONCURRENCY_EXCEEDED`、`ROUND_BUDGET_EXHAUSTED`。
- 恢复/一致性：`RECOVERY_INCONSISTENT`、`SQLITE_ERROR`、`MERGE_DECISION_DRIFT`。

## 本地验证结果

本次实际执行环境：Python `3.10.12`，Linux/WSL Ubuntu。

```bash
python3 -m unittest discover -v
# Ran 26 tests ... OK

python3 scripts/run_acceptance.py
# ok=true
```

`artifacts/acceptance_report.json` 记录了最近一次验收结果：

- 基线 18 个请求：`completed=11`、`rejected=4`、`started_failed=1`、
  `abandoned=1`、`not_started=1`。
- 基线语义摘要：`0c13653fdade9dc8aeea7f63849b1f70e54fc85f36066ae19b7110f139c88f20`。
- generation 3 增量切换后：`completed=9`、`rejected=6`、其余各 1；`analytics` 只影响
  `report`，`orders` 修订号不变。
- 资源事务中途退出（79）并恢复后：请求结局、资源余量和语义摘要与无中断基线完全一致。
- 依赖阶段中途退出（79）并恢复后：语义摘要与无中断基线完全一致。
- 策略提交前退出（78）并恢复后：待处理作业被重放，策略代数为 3，失效事件与干净更新一致。
- 重复 `schedule`：摘要与终态不变，无重复实例或重复扣减。
- 两次 `verify` 均返回 `ok=true`。

## 测试

```bash
python3 -m unittest discover -v
python3 scripts/run_acceptance.py --report artifacts/acceptance_report.json
```

测试覆盖输入校验、哈希不匹配、DAG 环、时钟回拨、v1 换算、来源优先级、分支失效、
策略回滚、幂等调度、资源观测、账本链、资源/依赖/策略崩溃恢复、半提交账本修复、
双调度进程和调度/策略更新并发。

## 目录

```text
topodiag/            核心包
  validation.py      输入校验
  policy.py          v1/v2 解析、合并、增量覆盖
  closures.py        闭包、指纹、失效链
  storage.py         SQLite 模式与事务
  engine.py          策略构建、影子版本与原子切换
  scheduler.py       拓扑/资源裁决、调度、账本、恢复
  diagnostics.py     status、verify、语义摘要
tests/               26 项 unittest
scripts/             可复现端到端验收
bin/topodiag         可执行入口
docs/DESIGN.md       设计说明
```
