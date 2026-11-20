# 多环境部署清单互操作与收敛验证引擎

这是一个完全本地、确定性的部署清单验证引擎。它把 v1 单文件 YAML 与 v2 分片 JSON + SQLite 元数据合并为统一环境视图，并在注入逻辑时钟下完成：

- 跨环境参数一致性判定与首个分歧定位
- 依赖 DAG 上的最坏链路超时、有效资源上限计算
- v1 到 v2 的时间/参数语义迁移与幂等记录
- 清单包完整替换、增量追加、无需操作识别
- 版本回退拒绝或注入时钟窗口内的降级
- 分阶段异常注入、SQLite 原子提交、崩溃恢复与多进程幂等

项目不读取系统墙钟，不访问网络或真实集群。

## 快速开始

环境：Linux、Python 3.11、PyYAML、pytest。`uv` 仅用于准备解释器和虚拟环境。

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e . PyYAML pytest

make samples       # 可选：重建全部本地样本
make test
make acceptance
make verify
```

等价命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_acceptance.py
deploy-convergence verify --state artifacts/state.sqlite
deploy-convergence status --state artifacts/state.sqlite
```

验收报告写入 `artifacts/acceptance-report.json`。

## 目录

```text
deploy_convergence/
  loader.py        v1 YAML / v2 JSON+SQLite 加载与校验
  validation.py    参数声明和值类型校验
  migration.py     v1 到 v2 的统一语义迁移
  merge.py         跨版本裁决、来源追踪、冲突清单
  evaluate.py      跨环境容差、依赖 DAG 和派生量
  replacement.py   版本替换分类和回退策略
  store.py         SQLite 状态、暂存表、操作日志、文件锁
  engine.py        分阶段执行、异常处理、恢复
  cli.py           命令行入口
samples/
  v1/manifest.yaml
  v2/meta.sqlite + v2/shards/*.json
  v2_next/meta.sqlite + v2_next/shards/*.json
  config.json
  clock_steps.json
  injections.json
scripts/
  build_samples.py
  run_acceptance.py
tests/
artifacts/acceptance-report.json
```

## 样本语义

三个固定环境为 `development`、`staging`、`production`，固定初始逻辑时钟为 `1735693200000 ms`。

- v1 所有参数缺少绝对生效时刻，由环境 `logical_clock`、`clock_scale_ms=1000` 和 `rounding=floor` 换算。例如 `dev auth-service` 从 `1735689600.125` 换算为 `1735689600125 ms`。
- v1 的 `orders-api.timeout_ms=800` 与 v2 的 `850` 故意冲突，用于验证冲突项不丢失。
- v1 的 `auth-service` 在 development 与 v2 生效时刻相同，v2 因来源优先级胜出。
- v2 用 `legacy-worker` 墓碑覆盖 v1 的活动条目；v1 的 `retired-cache` 在注入时钟下已经过期。
- v1-only 的 `reporting-api` 使用 v2 元数据声明的 `strategy=rolling`、`max_burst=0` 默认语义降级。
- `payments-api` 的 production `retry_factor=1.35`，其余环境为 `1.10`，形成可定位的一致性分歧。
- 依赖链 `gateway -> payments-api -> ledger-api -> auth-service` 的最坏链路超时为 `2450 ms`。
- `v2_next` 是 `2.2.0` 完整替换包，同时把 production 重试倍率调整到 `1.10`，因此替换后三环境一致。

## 关键规则

### 校验与错误

v1 校验环境内服务名唯一、正整数副本/超时、有限正数重试倍率、资源字段、扁平 `params` 声明与服务字段一致。v2 校验分片 SHA-256、元数据代数下限和单调性、毫秒整数时间、参数类型、依赖列表及墓碑约束。

业务错误码：

| 错误码 | 含义 |
|---|---|
| `E_BAD_MANIFEST` | YAML/JSON/元数据结构、唯一性或字段约束非法 |
| `E_BAD_PARAM_TYPE` | 参数键或值与声明类型不匹配 |
| `E_DIGEST_MISMATCH` | v2 分片实际哈希与元数据不一致 |
| `E_GENERATION_CONFLICT` | 清单代数低于下限、非单调或冲突 |
| `E_DEPENDENCY_CYCLE` | 依赖图成环 |
| `E_DEPENDENCY_MISSING` | 依赖的上游服务不可用 |
| `E_CLOCK_ROLLBACK` | 注入逻辑时钟回拨超过容差或窗口 |
| `E_VERSION_ROLLBACK` | 清单版本回退被策略拒绝 |
| `E_MIGRATION` | v1 语义迁移受控失败 |
| `E_STATE_CORRUPT` | 状态库结构或提交链路损坏 |
| `E_IO` | 本地文件不可读或缺失 |

非法 v1/v2 条目在创建操作日志前失败，因此不会写入业务视图、操作步骤或时钟水位。

### 合并与来源

同一 `(environment, service)` 的候选按以下固定顺序裁决：

1. 更大的绝对生效时刻
2. 相同时选择更高的来源优先级（v2 高于 v1）
3. 再按清单代数、候选 ID 决定

每个参数保存 `source_version`、`source_file`、`source_effective_at_ms`、`source_generation`、`original_time`、`decision_basis`，并标记是否为兼容默认值。参数值、墓碑状态或依赖集合不一致时写入 `conflicts`，保留被丢弃一侧。

### 迁移

v1 迁移记录换算标度、舍入方向、原始时间、派生毫秒值、代数映射 `null -> 0`、上游映射 `null -> []`，以及 `expired_before/after`。重复迁移由 `migration_id` 和已应用标记保证幂等。默认语义声明保存在 v2 `package_meta.compatibility_defaults_json`。

### 一致性

固定检查顺序为 `timeout_ms`、`retry_factor`、`resource_limit`，环境顺序固定。数值使用 `Decimal(str(value))` 运算，资源上限按 `cpu_millicores`、`memory_mb` 顺序比较。输出首个分歧参数、涉及环境、最大差值、容差和非负余量。

### 依赖派生

每个环境先对去重后的上游列表排序，再做确定性 DFS 拓扑检测。链路超时为：

```text
chain_timeout(service) = own_timeout + max(chain_timeout(upstream))
```

有效资源上限为自身与各上游对应资源字段的逐项最小值。若超时并列，固定选择字典序更小的上游。

### 版本替换

清单包描述包含版本号、代数下限、分片哈希、参数摘要和输入摘要。分类结果：

- `initial`：无当前视图
- `noop`：版本号、代数下限、分片哈希与参数摘要全部相同
- `incremental`：同版本、代数不降且旧分片哈希仍保留
- `full_replace`：版本或分片集合发生变化
- `rollback_degraded`：回退被 `degrade` 策略接受

替换先写入 `staging_versions`、`staging_services`、`staging_blobs`，当前 `state.current_view_id` 保持不变；提交事务复制暂存数据、分配 `commit_seq`、推进逻辑时钟水位并一次性切换指针。`reject` 策略直接返回 `E_VERSION_ROLLBACK`；`degrade` 只允许注入时钟回退窗口内接受。

## 持久化与恢复

状态库使用 Python 内置 `sqlite3`、WAL 和 `synchronous=FULL`。主要表：

- `view_versions`、`view_services`、`view_blobs`：统一视图及合并/迁移/一致性/派生结论
- `staging_*`：替换暂存区
- `operations`、`operation_steps`：分阶段操作日志
- `clock_state`：注入逻辑时钟水位
- `state`：当前视图指针

阶段顺序：

```text
parse -> migrate -> merge -> evaluate -> stage -> switch
```

每一步输出和完成状态在同一事务提交。进程在 `merge` 或 `stage` 后强制终止时，重启会读取已完成步骤输出，仅执行未完成步骤。暂存阶段被终止时当前视图指针不变；`switch` 事务中断时 SQLite 回滚，恢复后重放 `switch`。

整个执行期持有 `<state.db>.lock` 文件锁，提交时使用 `BEGIN IMMEDIATE`。并发提交同一输入时，第一个进程提交，后续进程观察到当前指针已指向同一 `package_key`，直接返回幂等结果。

## 异常注入

`samples/injections.json` 默认全部关闭。测试和验收脚本只切换 `enabled`，不会修改清单文件：

| 阶段 | 类型 | 错误码 |
|---|---|---|
| parse | `malformed_yaml_field` | `E_BAD_MANIFEST` |
| parse | `shard_digest_mismatch` | `E_DIGEST_MISMATCH` |
| migrate | `migration_failure` | `E_MIGRATION` |
| merge | `generation_conflict` | `E_GENERATION_CONFLICT` |
| evaluate | `dependency_cycle` | `E_DEPENDENCY_CYCLE` |
| evaluate | `clock_rollback` | `E_CLOCK_ROLLBACK` |

## 实际验证结果

环境：Python `3.11.15`、PyYAML `6.0.3`、pytest `9.2.0`。

```text
.venv/bin/python -m pytest -q
23 passed
```

`scripts/run_acceptance.py` 输出 `ok=true`，关键结果：

- 基线步骤：`parse, migrate, merge, evaluate, stage, switch`
- 冲突项 21 个、迁移记录 27 个、兼容降级 18 个
- 首个分歧：`payments-api.retry_factor`，production `1.35`，development/staging `1.10`
- 最大差值 `0.25`，容差 `0.05`，超容差余量 `0.2`
- `gateway` 最坏链超时 `2450 ms`，有效资源 `250 CPU millicores / 256 MiB`
- 六类注入均返回预期错误码，当前视图和时钟水位不变
- `merge` 后强制终止：恢复仅执行 `evaluate, stage, switch`，重放 `parse, migrate, merge`
- 双进程并发：仅 1 个视图版本、`commit_seq=1`、1 个完成操作，另一进程幂等
- `v2_next`：`full_replace`，替换后一致性为 `true`
- 回退 `reject`：`E_VERSION_ROLLBACK`，当前视图保持为新版本
- 回退 `degrade`：`rollback_degraded`，暂存表清空且指针已原子切换

完整机器可读结果见 `artifacts/acceptance-report.json`。
