# 本地 GitOps 回滚与收敛控制器

这是一个只依赖 Python 3.10+ 标准库的确定性本地控制器。它读取 v1 扁平清单、v2 分片清单与 SQLite 元数据、集群实际状态 JSON，完成校验、跨代合并、回滚计划、收敛执行、强制终止恢复和一致性校验。

实现不使用 Docker、网络或第三方包。SQLite 只保存控制器运行状态和 v2 元数据，集群状态使用文件锁与临时文件原子替换。

## 快速开始

```bash
python3 --version                 # 需要 >= 3.10
python3 scripts/generate_samples.py --reset-runtime
python3 -m gitops_controller --project samples/project.json load
python3 -m gitops_controller --project samples/project.json plan --env dev
python3 -m gitops_controller --project samples/project.json rollback --env dev --budget-ms 10000
python3 -m gitops_controller --project samples/project.json status --env dev
python3 -m gitops_controller --project samples/project.json verify --env dev
```

也可以直接使用仓库根目录的 `./gitopsctl`，参数完全相同。

完整可重复演示：

```bash
python3 scripts/demo.py
```

演示会从固定种子重建样本，连续执行两轮完整流程并比较全部 JSON 输出。两轮摘要哈希相同，脚本退出码为 0；退出前会把样本恢复为初始状态。输出位于 `.demo/run-1` 和 `.demo/run-2`。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

当前验证结果：15 个测试全部通过，覆盖解析校验、跨代合并、版本回退拒绝、预算耗尽、失败/主动放弃、崩溃恢复、并发幂等与双轮输出一致性。

Windows 使用建议：

- 全程使用 `pathlib.Path`，项目配置中的路径均相对清单目录解析。
- SQLite 使用 WAL、`busy_timeout` 和事务；运行控制器前关闭可能占用 `metadata.db` 或 `runtime/controller.db` 的编辑器。
- 文件锁同时支持 POSIX `fcntl` 与 Windows `msvcrt`；若运行失败，先确认没有残留进程持有运行库。

## 项目结构

```text
gitops_controller/
  manifests.py      v1/v2 严格解析与校验
  merge.py          统一期望状态、来源裁决与时间迁移记录
  planning.py       版本检查、逆依赖回滚计划、稳定拓扑排序
  engine.py         预算、执行、失败分类、崩溃恢复
  store.py          SQLite 事务状态、合并缓存、运行水位
  cluster.py        带文件锁的本地集群状态更新
  controller.py     load/status/verify 与应用状态视图
  cli.py            六个 CLI 子命令
scripts/
  generate_samples.py
  demo.py
samples/
  project.json
  v1/manifest.json
  v2/metadata.db
  v2/shards/{dev,staging,prod}.json
  v2/shards/history/*.json
  cluster-state.json
tests/test_controller.py
```

## CLI

全局选项可放在子命令前或后：

```text
--project PATH       项目配置，默认 samples/project.json
--clock-ms INT       注入逻辑时钟（Unix 毫秒）
--tolerance-ms INT   逻辑时钟回拨容差
--compact            输出紧凑、排序后的 JSON
```

子命令：

- `load`：校验两代来源、持久化合并结果和迁移决策。
- `plan --env ENV`：输出稳定排序回滚计划、版本裁决和上游完成条件。
- `rollback --env ENV [--budget-ms N] [--operation-cost-ms N]`：执行回滚。
- `resume --env ENV`：继续最近一次未完成的轮次。
- `status [--env ENV ...]`：输出各应用收敛状态、提交版本、运行摘要和逻辑时钟水位。
- `verify [--env ENV ...]`：重新计算合并并检查持久化状态、集群状态和未完成操作；校验失败时退出码为 1。

测试钩子：

```bash
python3 -m gitops_controller --project samples/project.json \
  rollback --env prod --budget-ms 10000 --crash-after 2
python3 -m gitops_controller --project samples/project.json \
  resume --env prod --budget-ms 10000
```

`--crash-after N` 会在第 N 次集群写入完成后、SQLite 提交前调用 `os._exit(75)`，用于验证真正的半提交恢复。`--fail-app` 与 `--abandon-app` 可分别注入启动后失败和主动放弃。

所有成功结果和业务错误均为排序 JSON。业务错误示例：

```json
{
  "error": {
    "code": "E_VERSION_ROLLBACK",
    "details": {
      "app": "checkout-api",
      "committed_manifest_revision": 14,
      "target_manifest_revision": 10
    },
    "message": "target manifest version is lower than the committed version"
  },
  "ok": false
}
```

## 清单格式

### v1

`samples/v1/manifest.json` 是单文件扁平映射：

```json
{
  "format": "v1",
  "logical_clock_ms": 1763510400000,
  "manifest_generation": 10,
  "applications": {
    "mailer": {
      "image_version": "0.7.0",
      "replicas": 1,
      "effective_seconds": 300,
      "params": {
        "depends_on": ["identity-api"],
        "tier": "sidecar"
      }
    }
  }
}
```

校验规则：

- 应用名必须规范且唯一；重复 JSON key 也会被拒绝。
- 副本数是正整数。
- 镜像版本必须是语义版本（如 `1.2.3`、`2.0.0-rc.1`）。
- 相对生效秒数是非负整数。
- 依赖上游必须存在且整张图无环。

v1 相对时间使用注入逻辑时钟换算：

```text
absolute_effective_time_ms = injected_clock_ms + effective_seconds * 1000
scale_ms_per_unit = 1000
rounding = floor
```

即使上游字段已经是秒，实现仍显式记录毫秒标度和向下取整方向。

### v2

`v2/metadata.db` 的 `manifest_revisions` 表保存每个环境按 `sequence` 排列的代际历史：

```sql
CREATE TABLE manifest_revisions (
    environment TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    shard_file TEXT NOT NULL,
    shard_sha256 TEXT NOT NULL,
    PRIMARY KEY (environment, sequence),
    UNIQUE (environment, generation)
);
```

校验规则：

- 每个环境的 `sequence` 与 `generation` 必须严格递增。
- 元数据中的 `shard_sha256` 必须与实际分片字节哈希一致。
- 分片顶层环境、代数、逻辑时钟必须与元数据一致。
- 应用的 `effective_at_ms` 必须是非负整数毫秒，保持原值，不做换算。
- 应用名、副本数、依赖存在性和无环性与 v1 同样校验。
- `tombstone: true` 表示失效墓碑，合并后保留条目但不参与期望活动应用图。

## 合并裁决

同一应用同时出现在 v1/v2 时，按以下稳定顺序选择：

```text
logical_clock_ms 降序
> manifest_revision 降序
> source_format_priority（v2 > v1）
> source_id 字典序
```

因此逻辑时钟相同时，较新清单代数优先；代数也相同时 v2 优先。每个最终条目的 `decision.candidates` 保留所有候选的来源格式、代数、逻辑时钟、原始时间、规范化时间和排名，`provenance` 保留胜出来源。该设计保证不丢失只存在于任一格式的应用，也保证相同输入重复合并逐字段一致。

v1 候选的每条时间换算决策写入 SQLite `migration_decisions`，主键由决策内容哈希生成，重复加载不会重复写入。

## 版本与回滚语义

`manifest_revision` 是控制面清单代数。计划拒绝：

```text
target_manifest_revision < max(cluster.committed_manifest_revision,
                               controller committed revision)
```

这种情况返回 `E_VERSION_ROLLBACK`。回滚载荷中的 `image_version` 可以降低，这正是应用版本回滚的目的；被禁止的是清单代数向更旧的一代回退。版本裁决同时输出集群侧与控制器侧已提交代数，便于审计。

计划按依赖边构造“上游先于下游”的稳定拓扑序。每个操作包含：

- `trigger.kind`、`trigger.fields` 与来源格式/来源哈希；
- 目标镜像、副本数、清单代数和目标摘要；
- `upstream_rollback_condition`，要求所有上游已提交或已处于收敛态；
- 稳定排序的 `sequence` 与内容级 `idempotency_key`。

独立应用按应用名字典序确定 Kahn 队列顺序；相同输入的计划 `plan_id` 和 JSON 输出稳定。

## 执行、预算与恢复

每个操作成本由 `--operation-cost-ms` 注入。启动前检查：

```text
current_clock + operation_cost <= deadline
```

预算不足的剩余操作标记为 `not_started`，原因 `budget_exhausted`。启动后必定记录完整逻辑结束时刻；即使结束时刻超过预算，也不会被截断为超时。结局区分：

- `not_started`：未启动；可因预算或上游未提交。
- `failed_after_start`：已启动后失败，已计入完整操作成本。
- `abandoned`：已启动后按策略主动放弃。
- `committed`：集群写入成功且持久化提交完成。

SQLite 运行状态包括轮次水位、计划摘要、应用阶段、逻辑起止时钟和幂等键。执行步骤如下：

1. 事务内将操作从 `pending` 原子领取为 `running`，写逻辑开始钟。
2. 文件锁内读取集群 JSON，原子替换写入目标状态。
3. SQLite 事务写入唯一提交记录并标记 `committed`，推进环境逻辑时钟水位。

第 2 步后、第 3 步前进程被终止时，操作保持 `running`。恢复先比较集群实际值与目标摘要：若已匹配，只补写提交记录，不重复写集群；若不匹配，则重新领取并重放。已提交应用不会被重复回滚。测试验证中断恢复与不中断执行产生相同的 run、status、commit 记录和集群状态。

## 并发与幂等

- 环境运行状态各自独立，多个控制器可并行处理 dev/staging/prod。
- SQLite 使用 WAL、`BEGIN IMMEDIATE` 和 15 秒 busy timeout，读取方只能看到完整事务快照。
- `commits.idempotency_key` 是主键，相同应用、环境和目标内容只能写入一条提交记录。
- 合并缓存以 `merge_key` 为主键，相同输入只登记一次。
- 集群 JSON 的读取-修改-写入在跨进程文件锁内完成，并通过同目录临时文件 `os.replace` 原子发布。
- 每个环境有独立的 rollback 文件锁，覆盖“计划生成到轮次完成”，避免同环境控制器生成分叉计划；不同环境可并行。
- 操作领取带 owner PID；其他存活控制器不会抢占，已死亡控制器的 `running` 操作可在恢复时接管。

## 样本与验收结果

固定种子为 `20261121`，项目为 `local-feedback-demo`：

- dev：v2 代数 12 覆盖 v1，包含应用漂移、镜像版本降级、缺失应用、墓碑删除和已收敛应用。
- staging：v1/v2 逻辑时钟与代数相同，验证 v2 来源优先；`checkout-api` 集群已提交代数 14 高于目标 10，计划返回 `E_VERSION_ROLLBACK`。
- prod：v2 代数 13，可用于执行两次写入后强制终止并恢复。
- `mailer` 仅存在于 v1，用于验证旧格式应用不会在合并中丢失。
- v2 元数据包含历史分片，当前分片由 SQLite 哈希固定。

实际可重复演示结果：

```json
{
  "deterministic": true,
  "run_1_sha256": "41057dee4588016e8d092d7e0d4861e7a3e47a84eb9537adb940d7b99b1618a0",
  "run_2_sha256": "41057dee4588016e8d092d7e0d4861e7a3e47a84eb9537adb940d7b99b1618a0"
}
```
