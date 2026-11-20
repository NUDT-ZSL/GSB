# 本地多集群配置分发控制面

这是一个只依赖本地 JSON、Python 标准库和 SQLite 的确定性控制面。它把全局、区域、集群三层配置合并为每集群有效配置，计算链路超时、有效容量和加权采样率，追踪定点换算误差，并按逻辑时间完成有界并发分发裁决。全过程不访问网络、不依赖容器，也不读取墙钟时间。

## 快速开始

环境要求：

- Python 3.10+
- NumPy 1.24+
- 标准库 `sqlite3`、`json`

```bash
python3 -m pip install -r requirements.txt

python3 -m controlplane --db state/controlplane.sqlite3 load
python3 -m controlplane --db state/controlplane.sqlite3 distribute
python3 -m controlplane --db state/controlplane.sqlite3 status
python3 -m controlplane --db state/controlplane.sqlite3 explain --cluster use1-b
python3 -m controlplane --db state/controlplane.sqlite3 verify
```

`status`、`explain` 和 `verify` 的 JSON 使用排序键和稳定序列顺序。独立创建状态的两次完整运行会生成逐字相同的报告。

## 固定样本

| 文件 | 内容 |
| --- | --- |
| `samples/clusters.json` | 8 个集群、区域、优先级、容量单位换算、覆盖策略和确定性调度时延 |
| `samples/regions.json` | 区域依赖图；当前包含菱形依赖 |
| `samples/config_templates.json` | 全局、区域、集群三层数值模板 |
| `samples/distribution_policy.json` | 定点标度、容差、误差预算、并发/重试/逻辑时钟策略 |
| `samples/logical_clock.json` | 逻辑时钟起点与逐轮步进表 |

样本刻意覆盖以下可观测场景：

- `use1-a`/`use1-b` 的加权采样率处于严格容差内的近邻，产生精度敏感标记。
- `usw1-a` 明确禁止差异覆盖但样本包含差异，因而被主动放弃。
- `usw1-b` 首次下发失败，重试因逻辑时间预算耗尽而未启动。
- `aps1-a`/`aps1-b` 在预算内未启动，但不会被标记为超时。

## 子命令

```text
python3 -m controlplane [--db PATH] [--sample-dir DIR] COMMAND [OPTIONS]
```

- `load`：先完整校验样本，再持久化版本快照与逻辑时钟初值；非法样本不会创建或修改数据库。
- `distribute`：声明并计算缺失工作项，全部完成后原子提交轮次并替换当前指针。
- `resume`：恢复硬终止留下的待提交轮次，只计算缺失集群。
- `explain`：输出当前已提交轮次的完整合并、派生、误差链、精度敏感记录与调度轨迹。
- `status`：输出各集群配置版本、有效配置摘要、分发状态、精度敏感标记数和逻辑时钟水位。
- `verify`：校验 SQLite 指针、轮次范围、工作项、版本/结果哈希、逻辑时钟，并从样本重算比对。

常用选项：

- `distribute --cluster NAME`：多个进程可分别声明不相交集群，轮次范围仍是完整样本。
- `distribute --new-round`：在已有已提交轮次后显式创建下一轮。
- `distribute --interrupt-after N`：测试钩子，完成 N 个新工作项后直接 `os._exit(75)`，用于模拟强制终止。
- `--output PATH`：将同一份稳定 JSON 同时写入文件。

## 数值与裁决

配置数值在解析时保留 JSON 十进制字面量。定点换算不经过二进制浮点：

1. 将十进制值表示为整数分子/分母。
2. 用 NumPy `int64` 数组批量乘以标度并做向量化半偶舍入。
3. 保存原值、十进制原文、标度、定值、舍入方向和量化误差。

默认合并顺序固定为 `global -> region -> cluster`。每个参数记录来源层、被覆盖层、合并路径、候选定值和覆盖授权结果。派生量使用固定点整数运算：

- 最坏链路超时 = 目标区域聚簇超时 + 区域依赖祖先基线超时之和。
- 有效容量 = 容量上限定值 × 集群容量单位因子。
- 加权采样率 = 采样率定值 × 副本权重定值。

差分小于定点容差且会改变区域排序或覆盖选择的候选会被记录为精度敏感，并附带参与层、参数、标度和换算误差来源链。

## 调度与恢复

调度器按区域依赖波次运行，使用逻辑时间而非墙钟时间，并受全局并发上限、每轮时间预算和重试预算约束。时间预算只阻止新的启动；已启动尝试一定会运行到自身完成。结局固定区分为：

- `SUCCEEDED`
- `STARTED_FAILED`
- `NOT_STARTED`
- `ACTIVE_ABANDONED`

SQLite 使用工作项日志两阶段提交：工作项逐集群原子完成，但读取方只能看到 `current_pointer` 指向的已提交轮次。最终事务整体写入轮次结果、逻辑时钟并替换指针。进程退出后，已完成工作项被复用，未被领取或仅被领取的工作项按缺失部分恢复；文件锁串行化轮次创建和最终提交，SQLite 唯一键与事务保证重复提交幂等。

## 验证

```bash
python3 -m unittest discover -v
./scripts/demo_determinism.sh
```

当前实际验证结果见 `docs/VERIFICATION.md`，设计细节见 `docs/DESIGN.md`。
