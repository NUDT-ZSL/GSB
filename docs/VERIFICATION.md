# 实际验证记录

## 环境

```text
Python 3.10.12
SQLite 3.37.2
NumPy 1.26.4
Linux 本地工作区，无容器、无网络服务
```

固定样本版本：

```text
version_id = v420ac22097175cb3
source_hash = 420ac22097175cb33d4bfd0e0c1d16227f8d2fad02a92b36fc2b1f488d7e2b5e
```

## 自动化测试

命令：

```bash
python3 -m unittest discover -v
```

结果：20 项测试全部通过。覆盖：

- 定点半偶舍入、方向、量化误差和重复换算逐位一致。
- 三层合并顺序、来源、覆盖列表和路径。
- 差异覆盖允许/拒绝和主动放弃。
- 跨层不一致的定值、层与容差余量。
- 依赖路径、派生量、误差预算和来源链。
- 覆盖候选与区域派生近差值精度敏感标记。
- 调度并发、预算耗尽、重试阻断与已启动不截断。
- 非法条目业务错误码且不创建数据库或锁文件。
- 中断后恢复只算缺失工作、逻辑时钟不回退。
- 4 个进程并行处理不相交集群时只提交一个完整轮次。
- 结果哈希被篡改时 `verify` 返回失败。
- 新版本提交整体替换指针，旧版本再次分发被拒绝且不会回退指针。
- 旧版本待恢复轮次不能在新版本提交后反向覆盖当前指针。
- `resume` 只恢复真实存在的待提交轮次，不使用新数据库隐式创建状态。
- 两个独立状态库的完整 CLI 输出逐字一致。

## 分发结果

```text
round_id = r0001
status = COMMITTED
logical_clock = 10
dispatch_counts = {
  "SUCCEEDED": 4,
  "STARTED_FAILED": 1,
  "NOT_STARTED": 2,
  "ACTIVE_ABANDONED": 1
}
max_observed_concurrency = 3
finished_at_ms = 12
precision_sensitive_total = 3
```

三个精度标记由两条唯一敏感记录组成，其中成对记录同时挂到两个相关集群：

- `use1-a`/`use1-b` 的加权采样率区域排序近差值。
- `use1-b` 的 `sampling_rate` 区域/集群覆盖候选近差值。

`usw1-a` 因 `DIFFERENTIAL_OVERRIDE_DENIED` 主动放弃。`usw1-b` 在 `t=5ms` 启动，首次尝试 `t=12ms` 失败；重试因 deadline 为 `11ms` 而未启动，结局为启动后失败。`aps1-a`/`aps1-b` 在预算内未启动。

关键派生量示例：

```text
aps1-a.worst_link_timeout_ms = 9300
aps1-a.dependency_path = ["us-east", "eu-central"]
aps1-a.effective_capacity = 204800 GiB
aps1-a.weighted_sampling_rate = 0.6
```

## 两次独立运行

运行：

```bash
./scripts/demo_determinism.sh
```

两个独立临时状态库均先 `load`，再 `distribute`、`status`、`explain`、`verify`。逐字比较结果：

```text
identical load       sha256=19e760ca75fbcff284fce4606f13ff068fdaf1232a178ce55fe614ca6849c60c
identical distribute sha256=b29708be94dadefef69a374c037920b0802b9393a9e09ad74efa183f0ff66507
identical status     sha256=c801afb7ea46b3343bc00a956221dfdbc58b26c84f36905aebd9d79e086c58c4
identical explain    sha256=0570636f6c210190cfc1f9e2ec08f34755ef4ee705ceafd644267754c4bc1096
identical verify     sha256=88c55f6cda3feb9a33babb4f8dbc6677120c897dece295768ce715de07772600
```

## 强制终止恢复

独立状态库执行：

```bash
python3 -m controlplane --db state.db load
python3 -m controlplane --db state.db distribute --interrupt-after 3
python3 -m controlplane --db state.db resume --report-work
python3 -m controlplane --db state.db verify
```

中断进程返回 `75`。中断后：

```text
current_pointer = 空
work_items = COMPLETE: 3, PENDING: 5
pending round = r0001
status.clusters = []
```

恢复结果：

```text
computed_this_run = 5
reused_this_run = 3
status = COMMITTED
round_id = r0001
logical_clock = 10
verify_ok = true
```

随后再次 `distribute` 返回同一 `r0001` 和同一逻辑时钟，证明提交幂等。

## 多进程并发

4 个控制面进程分别声明：

```text
use1-a,use1-b
usw1-a,usw1-b
euc1-a,euc1-b
aps1-a,aps1-b
```

最终数据库：

```text
rounds = [("r0001", "COMMITTED", 10)]
work_items COMPLETE = 8
round_clusters = 8
verify_ok = true
```

## Verify 内容

`verify` 检查：

- 当前指针与轮次版本、轮次号、逻辑时钟一致。
- 版本快照规范哈希与 `source_hash` 一致。
- 当前轮次结果覆盖完整预期集群。
- 所有工作项完成。
- 每集群结果摘要哈希和 `round_clusters.summary_hash` 一致。
- 无混合配置版本、所有结果共享逻辑时钟。
- 版本逻辑时钟不落后于当前指针。
- 从本地样本重算合并、派生、敏感裁决和调度，逐集群比对暂存工作与已提交结果。
