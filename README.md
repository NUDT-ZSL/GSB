# 票务规则传播与核验内核

这是一个仅依赖 Python 3 标准库的本地可复现实现。它从一个固定种子配置工程出发：

- 解析并校验场次规则、闸机订阅和入口/座席区拓扑；
- 计算受规则变更影响的闸机与多场次规则冲突；
- 按依赖 SCC 和拓扑层生成传播批次；
- 在 SQLite 中逐批事务提交“待传播、传播中、已生效、传播失败”状态机；
- 对重试失败和永久失败执行冻结、重推与降级；
- 为每条入场请求按闸机生效版本给出可追溯判定；
- 用完全独立的 `reference_judge.py` 重算全部结果并逐项比较。

## 快速开始

仓库已包含种子 `42` 生成的 `sample_project/`。运行完整验收：

```bash
python3 -m ticket_gate run \
  --project sample_project \
  --db runtime/run.db \
  --out runtime/kernel \
  --reference-out runtime/reference/reference.json \
  --reset
```

运行全部测试：

```bash
python3 -m unittest discover -s tests -v
```

重新生成固定种子工程，生成结果是逐字节确定的：

```bash
python3 -m ticket_gate generate --seed 42 --out sample_project --overwrite
```

环境中的 `sqlite3` 命令行不是必需的；持久化使用 Python 标准库 `sqlite3`。

## 固定样本

`sample_project/` 包含 12 个场次、48 台闸机、4 个变更场次和 126 条核验请求。种子 `42` 的已知结果：

- 受影响闸机 18 台；
- 传播计划 9 批，其中 1 个 SCC 含 6 台闸机，环上闸机同批处理；
- 1 台闸机同时订阅多个场次且新规则策略冲突；
- `GATE-001` 连续两次可重试失败后在第 2 批生效；
- `GATE-004` 首次永久失败，冻结在 `v1`，进入最终降级清单；
- 核验结果覆盖 `ALLOWED`、`TICKET_NOT_WHITELISTED`、`BEFORE_ENTRY_WINDOW`、`AFTER_ENTRY_WINDOW`、`REENTRY_NOT_ALLOWED`、`CHILD_REQUIRES_ADULT`。

## 命令

```text
python3 -m ticket_gate generate  --seed 42 --out sample_project [--overwrite]
python3 -m ticket_gate validate  --project sample_project
python3 -m ticket_gate plan      --project sample_project [--db runtime/run.db] [--out plan.json]
python3 -m ticket_gate propagate --project sample_project --db runtime/run.db [--reset]
python3 -m ticket_gate verify    --project sample_project --db runtime/run.db
python3 -m ticket_gate report    --project sample_project --db runtime/run.db --out runtime/kernel
python3 -m ticket_gate reference --project sample_project --out runtime/reference/reference.json
python3 -m ticket_gate compare   --kernel runtime/kernel --reference runtime/reference/reference.json
```

`run` 依次执行传播、核验、报告、独立参考计算和比较；`--reset` 从头创建数据库。

## 配置工程

`manifest.json` 的 `files` 字段指向所有配置。主要结构：

```text
manifest.json
config/events.json                  # 每个场次的 v1/v2 规则
config/gates.json                   # 闸机、入口、座席区、订阅场次
config/topology.json                # 入口与座席区有向依赖
config/changes.json                 # 场次旧版本 -> 新版本
config/faults.json                  # 按全局尝试序号注入故障
config/verification_requests.json   # 入场核验请求
```

场次规则字段：

```json
{
  "ticket_whitelist": ["ADULT", "CHILD"],
  "entry_window": {
    "start": "2026-12-12T18:00:00+08:00",
    "end": "2026-12-12T21:30:00+08:00"
  },
  "reentry_policy": "single",
  "child_policy": {"requires_adult": true}
}
```

`reentry_policy` 支持 `single` 与 `unlimited`。解析器为五个规则叶子字段分别保留 JSON Pointer 来源路径，例如：

```text
config/events.json#/events/EVT-04/versions/v2/rules/entry_window/start
```

字段缺失、时间格式非法或 `start > end` 会定位到原始来源路径并拒绝该场次。只要工程仍有校验问题，传播命令拒绝启动；`validate` 会输出全部问题。

## 传播语义

1. 受影响闸机是所有直接订阅至少一个变更场次的闸机。
2. 对每个入口依赖 `upstream -> downstream`，下游入口的所有闸机依赖上游入口的所有闸机；座席区依赖同理。依赖边保留 `entrance:<id>` 或 `seat_zone:<id>` 原因。
3. 只保留受影响闸机之间的依赖边，使用 Tarjan 算法求 SCC。SCC 被视为一个调度节点，因此环上闸机进入同一批；环闸机 ID 单独列出。其余 SCC 用 `heapq` Kahn 分层，层号即批次号，批内节点互不依赖。
4. 每台闸机初始保存订阅场次的旧版本组合，目标保存新版本组合。状态迁移为：

```text
pending -> propagating -> active
                       -> failed (retryable) -> propagating -> ...
                       -> failed (terminal, 保持旧版本)
```

5. 可重试故障写入 `retry_queue`，到期批为当前批 `+1`。到下一批时与计划批次闸机一起重推；最后计划批仍有重试时自动追加扩展批。永久失败不再重试，`active_versions` 保持最后一已生效组合。
6. 每个执行批在一个 `BEGIN IMMEDIATE` 事务内完成状态迁移、尝试记录、批次开始/完成时间与完成标记。只有提交后的状态对重启可见。

同一闸机同时订阅多个场次时，如果任意两场次满足以下全部条件，则标为冲突：

- 两个场次的新版本入场时间窗严格重叠；
- 票种白名单有交集；
- `reentry_policy` 或 `child_policy.requires_adult` 不同。

输出同时给出冲突字段和两侧规则来源路径。该定义也写在生成工程 `manifest.json` 的 `conflict_definition` 中。

## 核验与降级

核验请求的 `after_batch` 语义：

- 整数 `N`：取第 `N` 批完成后的闸机状态和版本快照，不会读取后续批次结果；
- `null`：取最终状态。

判定顺序固定为：

1. 票种白名单；
2. 早于入场时间窗；
3. 晚于入场时间窗；
4. 儿童票需成人同行；
5. 重复入场策略；
6. 全部通过。

每条拒绝记录含决定性的 `field`、原始 `source` 和规则值；允许记录也包含全部已检查字段。只有当闸机状态为 `active` 且其全部订阅场次版本组合等于目标组合时，`rule_mode` 才为 `current`。否则为 `degraded`，按快照中的冻结版本判定，不会宣称已按新规则核验。

最终降级清单仅包含冻结组合与最新组合不一致的闸机，并逐场次列出：

- 冻结版本、最新版本；
- 发生变化的规则字段；
- 两侧字段值和原始来源路径；
- 降级原因（永久失败、待重试或尚未生效）。

## SQLite 账目

核心表：

- `gate_state`：逐闸机目标、生效、初始版本，状态和尝试计数；
- `gate_transitions`：每次状态迁移及发生批次、尝试号、错误；
- `plan_batches`：基础依赖批次和环闸机；
- `batch_runs`：实际执行批开始/完成时间、基础闸机、重试闸机和尝试闸机；
- `propagation_attempts`：每次投递的规则版本、成功/失败和错误；
- `retry_queue`：可重试闸机及到期批；
- `version_snapshots`：每台闸机的初始快照与生效批次快照；
- `verification_ledger`：逐请求判定账目。

## 输出文件

`report --out`/`run --out` 生成：

```text
summary.json           # 完整账目与不变量
plan.json              # 受影响集合、依赖、批次、环、执行批
gate_versions.json     # 逐闸机目标/生效版本和状态
degradation.json       # 降级闸机与字段级差异
decisions.jsonl        # 每行一条核验判定
decision_stats.json    # 按结果、原因码和规则模式计数
audit.json             # 全部状态迁移与投递尝试
compare.json           # run 时自动与独立参考结果比较
```

`summary.json` 的 `invariants` 会检查：

- 状态账目与迁移账目中的闸机集合等于受影响集合；
- 每条基础批次都有已完成执行记录；
- 重试队列为空；
- 每台闸机生效版本键与目标版本键一致；
- 核验账目中的闸机均属于受影响集合。

## 独立参考与中断复现

`reference_judge.py` 不导入 `ticket_gate`，只读取 JSON，自行重算受影响集合、依赖、SCC 批次、故障重试、快照版本、降级差异和每条核验判定。`compare` 逐项比较：

- 受影响闸机集合；
- 依赖边与批次成员、环；
- 多场次冲突；
- 逐闸机最终状态与版本；
- 降级场次与规则字段；
- 每条判定和统计。

在指定批完成并提交后强制退出：

```bash
python3 -m ticket_gate run \
  --project sample_project \
  --db runtime/recovery.db \
  --out runtime/recovery-kernel \
  --reference-out runtime/recovery-reference/reference.json \
  --reset \
  --crash-after-batch 1

# 退出码为 75；此时仅第 0、1 批已提交
python3 -m ticket_gate run \
  --project sample_project \
  --db runtime/recovery.db \
  --out runtime/recovery-kernel \
  --reference-out runtime/recovery-reference/reference.json
```

重启后已提交批次不会重复执行，`GATE-001` 从第 1 批的失败状态继续，在第 2 批成功；最终判定与无中断运行一致。中断测试已包含在 `tests/test_recovery.py`。
