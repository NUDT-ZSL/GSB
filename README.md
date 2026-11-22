# 本地多集群配置分发引擎

这是一个不依赖 Kubernetes、`kubectl`、容器或网络端口的本地确定性配置分发引擎。它从全局基线、区域覆盖层和集群覆盖层构建每个成员集群的有效配置，并在审批门禁、配额账本、逻辑时钟、变更窗口和并发上限的共同约束下生成可审计的下发裁决。

## 快速开始

要求 Python 3.10+（已按 Python 3.11 目标实现并兼容 3.10）以及 PyYAML：

```bash
python -m pip install -r requirements.txt
```

清空本地状态后执行完整流程：

```bash
rm -rf state
python -m mcde load
python -m mcde merge
python -m mcde plan
python -m mcde apply
python -m mcde status
python -m mcde verify
```

Windows PowerShell 对应清空命令为：

```powershell
Remove-Item -Recurse -Force state -ErrorAction SilentlyContinue
```

也可以运行跨平台演示脚本，它会自动清空 `state/` 并依次执行全部子命令：

```bash
python scripts/demo.py
```

全局参数 `--db`、`--samples`、`--lock-dir` 既可放在子命令前，也可放在子命令后：

```bash
python -m mcde --db state/demo.sqlite3 load
python -m mcde load --db state/demo.sqlite3
```

所有命令只执行本地文件与 SQLite 操作，不监听端口，不访问网络。

## 样本结构

`samples/manifest.json` 将不同格式的样本组合起来：

- `global.yaml`：全局应用基线。应用名在全局层唯一；字段为 `image`、`replicas`、`resources`、`timeout_ms`、`feature_flag`。
- `regions.json`：区域覆盖层。`allow_diff_overrides` 控制该区域是否允许集群层产生不同值。
- `clusters.yaml`：集群覆盖层。包含所属区域、覆盖值、集群副本上限与资源配额。
- `inventory.json`：成员集群清单。包含区域、健康标记、最大副本总量与最大资源总量。
- `rules.yaml`：审批要求与 TTL、变更窗口、每轮并发上限和每轮逻辑时间预算。
- `approvals.json`：审批记录。审批固定 `subject`、候选值、区域/集群范围与逻辑时间有效期。
- `clock.json`：可注入逻辑时钟步进表。每对 `dispatch_start` / `dispatch_finish` 构成一次可恢复的下发步进。

样本中的 `resources` 是应用级资源单位；集群资源账本总量为各应用生效 `resources` 之和，副本账本总量为各应用生效 `replicas` 之和。

主样本故意覆盖了四类结果：

- `east-a`、`south-a`、`south-b` 成功下发。
- `east-b` 因审批过期拒绝，`east-c` 因审批范围不匹配拒绝。
- `west-a` 在禁止差异覆盖的区域产生越权覆盖冲突。
- `north-a` 因副本/资源配额溢出拒绝。
- `west-b` 在启动后因集群健康标记失败，属于 `failed_after_start`。

`samples/negative/` 还包含未知区域引用、集群区域归属错误、类型错误、非法上限、越权覆盖和配额溢出样本。

## 合并与裁决语义

每个应用字段按 `global -> region -> cluster` 顺序合并。最终结果中的每个键都包含：

- `value`：实际生效值；越权或未获审批的集群覆盖保持继承值。
- `proposed_value`：最高层候选值，用于审计被拒绝的覆盖。
- `source_layer`：生效值来源层。
- `overridden_layers`：被生效值覆盖的低层列表。
- `merge_path`：按层记录的全部显式值。
- `policy`：区域差异策略、审批匹配结果与审批 ID。

集群覆盖值若与区域有效值不同：

1. 区域 `allow_diff_overrides=false` 时产生 `E_OVERRIDE_CONFLICT`，集群整体拒绝。
2. 区域允许差异但规则要求审批时，必须匹配 subject、候选值、区域、集群和逻辑时钟有效期。
3. 审批过期、尚未生效、范围不匹配或已被消费分别返回独立错误码。

区域层相对全局层的适配由区域策略本身授权；审批门禁作用于区域层与集群层之间的差异化覆盖。

配额检查按应用名排序累计副本与资源。任一总量超过有效上限即拒绝集群，并输出首个触发键、容量、请求前余量、溢出量、请求后余量。有效上限取成员清单容量与集群覆盖上限的较小值。

## 持久化与恢复

SQLite 使用 WAL、`BEGIN IMMEDIATE`、外键、CHECK 约束和跨进程文件锁。主要表包括：

- `config_versions` / `active_version`：完整版本与新版本指针。
- `cluster_commits` / `cluster_state`：每集群版本、已提交有效配置与回读配置。
- `ledger`：副本和资源账本，数据库约束保证任何时刻不为负且不超过容量。
- `approval_uses`：审批消费唯一记录，主键保证同一审批只消费一次。
- `rounds` / `dispatches` / `dispatch_events`：轮次水位、三类调度结局与状态事件链。
- `clock_state`：逻辑时钟水位和步进表游标。
- `audit_log`：版本、轮次、集群提交和失败审计事件。

新版本提交时先在连接级临时表 `_version_stage` 中写入全部集群裁决，校验数量和摘要，再在同一个事务内插入完整版本并整体替换单行 `active_version` 指针。请求版本号不大于当前活动版本时返回 `E_VERSION_ROLLBACK`。

强制终止后可恢复的注入点：

```bash
MCDE_CRASH_AT=after_version_stage   python -m mcde apply
MCDE_CRASH_AT=after_version_commit  python -m mcde apply
MCDE_CRASH_AT=after_dispatch_start  python -m mcde apply
MCDE_CRASH_AT=after_approval_consume python -m mcde apply
MCDE_CRASH_AT=after_ledger_update   python -m mcde apply
MCDE_CRASH_AT=after_cluster_commit  python -m mcde apply
```

进程在账本事务内退出时，SQLite 回滚整个账本、审批和配置提交事务。若 `started` 标记已单独提交，`resume` 只补做完成/提交步骤，不重新启动、不会把已启动下发截断为超时，也不会重复扣减账本或消费审批。

`merge` 与 `plan` 是纯内存只读阶段，不产生候选版本或账本中间态，因此在该阶段退出后重新执行即可。版本阶段、审批消费、账本扣减和集群下发阶段则都有上述可恢复边界。

## 调度结局

- `applied`：配置与账本、审批消费同一事务提交成功。
- `failed_after_start`：已记录启动时间，随后因健康或提交期错误失败。
- `not_started_budget`：逻辑时间预算不足以容纳完整启动+完成成本，未启动即放弃本轮。
- `abandoned`：操作者通过 `--abandon CLUSTER` 主动放弃。
- `rejected`：合并、审批或配额阶段拒绝。
- `queued`：变更窗口外排队，记录 `earliest_ms`。

启动前会检查完整的 start+finish 成本，因此一旦变成 `started`，后续完成不会受本轮预算截断。预算耗尽时本轮剩余集群记为 `not_started_budget`；后续 `resume` 可显式重试。

## 子命令

```text
load     校验全部样本并原子写入活动快照；校验失败不创建/修改状态
merge    只读构建三层合并、来源审计、冲突与配额裁决
plan     只读预览审批、窗口与轮次调度，不推进逻辑时钟
apply    提交版本并执行可运行的下发
resume   恢复已启动步骤并重试可恢复的未启动步骤
status   稳定排序 JSON，展示每集群版本、账本余量和冲突计数
verify   比对期望配置与回读配置，并输出每个残留键的依据链
```

`verify` 在每个残留键的 `basis` 中串联快照、合并路径、策略/审批、配额、调度结局和提交/回读版本。只有无法归因于显式拒绝、主动放弃或启动后失败的差异才标记为 `unexplained_drift`，此时命令退出码为 `4`。

## 主要错误码

| 错误码 | 含义 |
| --- | --- |
| `E_REF_REGION` | 区域层引用清单中不存在的区域 |
| `E_REF_CLUSTER` | 集群层或审批引用不存在的集群 |
| `E_REF_CLUSTER_REGION` | 集群声明的区域与成员清单不一致 |
| `E_REF_APP` | 覆盖层或审批引用不存在的应用/键 |
| `E_TYPE` | 字段类型不正确 |
| `E_LIMIT` | 上限、TTL、并发数或预算不是正整数等非法值 |
| `E_OVERRIDE_CONFLICT` | 策略不允许的差异覆盖冲突 |
| `E_APPROVAL_REQUIRED` | 缺少所需审批 |
| `E_APPROVAL_EXPIRED` | 审批在逻辑时钟上已过期 |
| `E_APPROVAL_SCOPE_MISMATCH` | 审批主体、值、区域或集群范围不匹配 |
| `E_APPROVAL_ALREADY_CONSUMED` | 同一审批记录已被消费 |
| `E_QUOTA_REPLICAS` | 副本总量配额溢出 |
| `E_QUOTA_RESOURCES` | 资源总量配额溢出 |
| `E_VERSION_ROLLBACK` | 请求版本回退 |
| `E_LOCK_TIMEOUT` | 跨进程文件锁等待超时 |

## 验证

运行全部自动化测试：

```bash
python -m unittest discover -s tests -v
```

测试覆盖解析错误码隔离、三层来源、审批过期/范围不匹配、越权冲突、配额首触发键、窗口排队、预算耗尽、主动放弃、幂等重放、版本回退、并发进程、回读漂移，以及 `after_dispatch_start` / `after_ledger_update` 两种强制终止恢复。

实际命令行验证结果见 `docs/VERIFICATION.md`。

