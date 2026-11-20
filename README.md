# kdrift：本地 Kubernetes 声明式配置漂移收敛引擎

`kdrift` 是一个完全本地、确定性的 Kubernetes 期望状态/实际状态漂移计算与收敛演示引擎。它不连接集群、不读取系统墙钟、不依赖容器运行时；所有资源、漂移场景和逻辑时间均来自 JSON 样本与注入配置。

实现覆盖：

- Deployment、Service、ConfigMap 的解析、业务校验、错误隔离。
- 缺失、多余、字段不一致、副本数不一致、选择器不匹配五类漂移。
- 小规模逐字段精确比对与跨阈值 Merkle 分层摘要采样比对。
- 摘要模式精确回退、召回率/误差上界报告、两种模式等价审计。
- ConfigMap 挂载、Service 选择器等引用依赖，创建/删除拓扑与删除安全裁决。
- 每轮最大并发、单对象超时、逻辑时钟、确定性调度与放弃原因。
- SQLite 原子轮次提交、幂等操作账本、强制终止恢复。
- `load`、`diff`、`plan`、`apply`、`resume`、`status`、`verify` 子命令。

## 环境

本项目要求 Python 3.11+、标准库 SQLite 和 NumPy。

本工作区实际验证环境：

```text
Python 3.11.15
sqlite3 3.53.1
numpy 2.4.6
```

依赖已安装在 `.venv/`。运行期不需要网络、Docker 或外部服务：

```bash
.venv/bin/python --version
.venv/bin/python -c "import sqlite3, numpy; print(sqlite3.sqlite_version, numpy.__version__)"
```

也可以使用根目录包装器：

```bash
./driftctl --help
```

## 快速演示

```bash
.venv/bin/python -m unittest discover -v
./scripts/demo.sh
```

若环境安装了 `make`，也可使用 `make test` / `make demo`；项目验收不依赖
`make`。

手工执行小样本：

```bash
./driftctl load --sample samples --db .kdrift/demo.db
./driftctl diff --db .kdrift/demo.db
./driftctl plan --db .kdrift/demo.db
./driftctl apply --db .kdrift/demo.db
./driftctl status --db .kdrift/demo.db
./driftctl verify --db .kdrift/demo.db
```

跨阈值样本：

```bash
./driftctl load --sample samples/scale --db .kdrift/scale.db
./driftctl diff --db .kdrift/scale.db
```

中断恢复演示：

```bash
./driftctl load --sample samples --db .kdrift/crash.db
set +e
./driftctl apply --db .kdrift/crash.db --crash-at operation-start:2
test $? -eq 137
set -e
./driftctl resume --db .kdrift/crash.db
./driftctl verify --db .kdrift/crash.db
```

可用崩溃注入点：

- `operation-start:N`：已持久化“收敛中”和并发槽、尚未执行侧效。
- `operation-commit:N`：操作账本、实际状态、槽位已原子提交。
- `round-commit:N`：本轮操作已执行，轮次水位和对象阶段尚未提交。

## 资源格式

样本均为稳定排序 JSON。资源使用扁平身份字段，便于确定性处理和教学演示：

```json
{
  "kind": "Deployment",
  "namespace": "shop",
  "name": "api",
  "replicas": 2,
  "image": "registry.local/shop/api:1.4.0",
  "labels": {"app": "api", "tier": "backend"},
  "selector": {"matchLabels": {"app": "api"}},
  "configMapRefs": ["api-config"],
  "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}]
}
```

Service 示例：

```json
{
  "kind": "Service",
  "namespace": "shop",
  "name": "api-svc",
  "labels": {"app": "api"},
  "selector": {"matchLabels": {"app": "api"}},
  "ports": [{"port": 80, "targetPort": 8080, "protocol": "TCP"}]
}
```

ConfigMap 示例：

```json
{
  "kind": "ConfigMap",
  "namespace": "shop",
  "name": "api-config",
  "labels": {"app": "api"},
  "data": {"LOG_LEVEL": "info", "TIMEOUT_MS": "5000"}
}
```

选择器既支持 Kubernetes 结构的 `matchLabels`/`matchExpressions`，也支持字符串：

```text
app in (api,worker),tier=backend,!disabled
```

对象身份为 `kind/namespace/name`；样本解析额外要求 `namespace/name` 组合全局唯一，跨三类对象重复会返回 `E_DUP_KEY` 并隔离所有重复项。

实际状态中的 Deployment `configMapRefs` 是硬引用。引用的同命名空间 ConfigMap 不存在时，Deployment 被隔离并返回 `E_DANGLING_REF`。Service 选择器是软引用，不满足时作为 `selector_mismatch` 漂移而不是非法对象。

## 收敛配置

```json
{
  "max_concurrency": 3,
  "operation_timeout_ms": 100,
  "exact_object_threshold": 12,
  "exact_field_threshold": 12,
  "sampling_ratio": 0.5,
  "tolerance": 0,
  "logical_step_ms": 25,
  "initial_logical_ms": 1000,
  "retry_limit": 1,
  "retry_backoff_ms": 10,
  "equivalence_audit": true,
  "strata_count": 12,
  "simulated_failures": [],
  "operation_durations_ms": {}
}
```

- `max_concurrency`：每轮最多启动的变更操作数；`noop`/维持操作不占该预算。
- `operation_timeout_ms`：单对象操作超时预算。
- `exact_object_threshold`：单命名空间对象数阈值，严格超过时切换摘要模式。
- `exact_field_threshold`：单对象字段数阈值，严格超过时切换摘要模式。
- `sampling_ratio`：摘要分层首轮采样比例，范围 `(0, 1]`。
- `tolerance`：非副本数值字段容差；`replicas` 始终保持精确判定。
- `logical_step_ms`：默认单操作逻辑耗时。
- `initial_logical_ms`：首次加载的逻辑时钟。
- `retry_limit`：首次失败后的最大重试次数。
- `retry_backoff_ms`：指数退避基数，重试时间完全由逻辑时钟计算。
- `equivalence_audit`：摘要模式是否并行运行精确参考实现并比较判定。
- `strata_count`：每个深度/尺寸层内允许的路径摘要桶数上限。
- `simulated_failures`：资源键列表，如 `Deployment/shop/api`。
- `operation_durations_ms`：资源键到逻辑毫秒耗时的覆盖表。

## 差异输出

漂移类型固定为：

- `missing`
- `extra`
- `field_mismatch`
- `replicas_mismatch`
- `selector_mismatch`

每个漂移包含稳定对象身份、字段路径、期望值、实际值、存在标志和判定依据。排序键为：

```text
namespace, kind order, name, field_path, type
```

JSON 使用 `sort_keys=True`、固定缩进、UTF-8 原字符，不使用墙钟时间或随机数。相同数据库重复执行 `diff` 会产生逐字节一致输出。

## 精确与摘要模式

精确模式逐叶字段比较。字段路径采用 `$.labels.app`、`$.ports[0].port` 形式，缺失值与 JSON `null` 由 `expected_present`/`actual_present` 区分。

触发任一条件时切换为 `digest_sampled`：

```text
单命名空间对象数 > exact_object_threshold
单对象字段数 > exact_field_threshold
```

摘要模式流程：

1. 对每个字段的值和存在性计算 SHA-256。
2. 用 NumPy 分位数把字段长度分层为 small/medium/large，再按路径摘要分桶形成分层摘要。
3. 生成对象 Merkle 根摘要；根相等即可证明无字段漂移。
4. 根不等时，按 `sampling_ratio` 做确定性分层采样，优先发现常见漂移。
5. 为防止采样漏检，对未采样层执行完整回退遍历；最终差异集合与精确模式完全一致。

输出中的证据包含：

- `sampled_strata`、`completion_strata`
- `sample_only_diff_count`、`completion_diff_count`
- `sampling_coverage`、`expected_single_stratum_detection_probability`
- `sample_only_recall_estimate`
- `final_recall_estimate`（回退遍历后为 `1.0`）
- `false_negative_upper_bound`（回退遍历后为 `0.0`）
- `equivalence`：摘要与精确路径的漂移签名对比结论

这里把“采样”用于首轮成本取舍，同时用 Merkle 根变化触发完整遍历，从而在可证明的意义上保证两种模式对同一漂移判定等价。

## 计划与依赖

计划动作：

- `create`
- `delete`
- `update`
- `noop`（`semantic_action=maintain`）

依赖规则：

- Deployment 引用的 ConfigMap 先创建/更新。
- Service 选择器命中的 Deployment 先创建/更新。
- 删除 Service 优先于其选择的 Deployment。
- 删除 Deployment 优先于其挂载的 ConfigMap。
- 删除前若仍有存活引用，且引用方既未删除也未更新移除引用，则计划返回 `E_UNSAFE_DELETE`，不会提交执行。
- 依赖边同时登记为互斥边，保证关联操作不会在同一逻辑批次并发启动。
- 每个对象有唯一 `exclusive_key`；冲突内容输出到 `mutual_exclusion_pairs`。
- 依赖图存在环时返回 `E_PLAN_CYCLE`。

`plan.valid=false` 时 `apply` 以 `E_PLAN_INVALID` 拒绝执行。

## 轮次状态机

对象阶段：

```text
pending -> converging -> converged
                     \-> failed -> pending/converged
                     \-> abandoned
```

每轮在拓扑可启动操作中，按配置最多启动 `max_concurrency` 个变更操作。同一批操作共享逻辑开始时间，并按命名空间分组记录。每轮完整结果区分：

- `committed`：成功提交。
- `not_started_budget`：预算已耗尽，未启动，留给后续轮次。
- `not_started_dependency`：先决操作尚未提交。
- `not_started_retry_backoff`：逻辑时钟尚未到达退避时间。
- `started_failed`：已启动后超时或注入失败，不是“未启动即超时”。
- `abandoned`：重试耗尽或先决操作已放弃，显式终止。

超时、退避和轮次逻辑结束时间只使用 SQLite 中的 `logical_clock_ms`。代码不读取 `time.time()`、墙钟或环境时钟。

## SQLite 与恢复

核心表：

- `meta`：逻辑时钟、轮次水位、下一轮号、状态。
- `desired_objects` / `actual_objects`：规范化资源与内容摘要。
- `object_state`：阶段、模式、失败次数、退避时间、放弃原因。
- `rounds`：轮次开始/结束逻辑时间、状态、完整摘要。
- `operations`：本轮操作名册、状态、尝试次数、结果。
- `committed_operations`：全局幂等账本，每个操作只允许一次。
- `concurrency_slots`：当前逻辑并发占用。

操作提交事务原子包含：

1. 检查幂等账本；
2. 创建/更新/删除实际对象；
3. 写入 `committed_operations` 结果摘要；
4. 更新操作状态；
5. 释放并发槽；
6. 推进逻辑时钟。

轮次提交事务原子包含轮次水位、对象阶段、完整结果摘要、并发零占用校验和逻辑时钟。进程被 `SIGKILL`/`os._exit` 终止时 SQLite 事务要么完整提交，要么回滚。

`resume` 的恢复步骤：

- 读取持久化槽位和半提交操作，不依赖内存。
- 释放失效槽位。
- 将未提交的 `executing` 轮次标记为 `aborted_recovered`。
- 已进入账本的操作永不重复执行。
- 未进入账本的操作重新生成计划，操作 ID 稳定且提交路径再次检查账本。

## 一致性校验

`verify` 检查：

- 无活跃并发槽、无 `started/planned` 操作、无执行中轮次。
- 操作账本无重复，提交记录与账本双向一致。
- 账本结果摘要可重算且一致。
- 持久化轮次水位等于最大已提交轮次。
- 实际资源重新解析后引用闭合。
- 对象阶段与重算差异一致。
- 残余差异集合为空，或每个残余对象都在 SQLite 中被显式标记为 `abandoned`。

每个风险对象输出三段判定链：重算差异证据、SQLite 阶段/原因、账本与逻辑水位。

## 子命令

```text
kdrift load   --sample DIR | --expected F --actual F --config F [--db F]
kdrift diff   [--db F]
kdrift plan   [--db F]
kdrift apply  [--db F] [--max-rounds N] [--crash-at POINT]
kdrift resume [--db F] [--max-rounds N] [--crash-at POINT]
kdrift status [--db F]
kdrift verify [--db F]
```

退出码：

- `0`：成功或一致性通过。
- `1`：计划不安全、未收敛或 `verify` 不通过。
- `2`：配置、文件、数据库或 CLI 错误。
- `137`：确定性崩溃注入。

## 业务错误码

- `E_BAD_RESOURCE`
- `E_DUP_KEY`
- `E_BAD_SELECTOR`
- `E_DANGLING_REF`
- `E_BAD_CONFIG`
- `E_PLAN_CYCLE`
- `E_UNSAFE_DELETE`
- `E_PLAN_MISSING_REF`
- `E_PLAN_INVALID`
- `E_STATE_CORRUPT`
- `E_NOT_LOADED`
- `E_BAD_JSON`

非法对象只产生校验问题和隔离记录；其余合法对象继续参与漂移计算。

## 样本

- `samples/`：小规模精确模式，10 个漂移，覆盖全部五类。
- `samples/scale/`：单命名空间对象数跨阈值，10 个漂移，触发分层摘要采样。
- `scripts/build_samples.py`：使用 `kdrift.simulator` 从期望状态确定性生成实际状态。
- 样本生成过程不读取墙钟、不使用随机数。

## 实际验证结果

已执行：

```text
python -m unittest discover -v
18 tests, OK

scripts/demo.sh
small diff: exact, 10 drifts, repeated output byte-identical
small apply: converged, verify=ok
scale diff: digest_sampled, exact-equivalent, final recall=1.0, FN bound=0.0
crash operation-start:2: exit 137, resume succeeded, verify=ok
crash round-commit:1: exit 137, resume succeeded, verify=ok
```

测试还覆盖注入失败、超时、并发预算推迟、未安全删除拒绝、摘要/精确等价、重复提交幂等和阶段一致性。
