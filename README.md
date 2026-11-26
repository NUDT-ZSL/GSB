# coord-workbench — 协调选举与故障转移诊断工作台

面向协调中间件独立验证的领域可视化与诊断工作台，覆盖选主拓扑归并、
租约世代推进与故障转移决策。全部输入由固定种子脚本确定性生成，不依赖
外部网络或真实服务。原始任务说明见 `docs/TASK.md`。

## 环境准备（WSL Ubuntu）

```bash
sudo apt install python3 python3-pip   # Python >= 3.10
pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成确定性样本（nodes/events/subscriptions/topo_v1/topo_v2）
python3 -m coord_workbench.cli gen-samples samples

# 2. 校验：分批归并 vs 一次性重放必须逐项一致
python3 -m coord_workbench.cli verify --samples samples

# 3. 执行归并（调度器受并发上限与逻辑时间预算约束）
python3 -m coord_workbench.cli merge --samples samples --workdir run \
    --max-concurrency 16 --budget 100000

# 4. 查询稳定排序的状态 JSON（领导者/租约世代/字段统计/缓冲状态）
python3 -m coord_workbench.cli state --samples samples --workdir run

# 5. 崩溃恢复演练：第 3 批提交中途模拟 SIGKILL，再恢复
python3 -m coord_workbench.cli merge --samples samples --workdir run_crash \
    --crash-after-batch 3          # 退出码 137
python3 -m coord_workbench.cli recover --samples samples --workdir run_crash
python3 -m coord_workbench.cli merge  --samples samples --workdir run_crash

# 6. 启动诊断接口
CWB_WORKDIR=run CWB_SAMPLE_DIR=samples \
    python3 -m uvicorn coord_workbench.api:app --host 127.0.0.1 --port 8000
# 或： python3 -m coord_workbench.cli serve --samples samples --workdir run
```

## 诊断接口

| 端点 | 说明 |
| --- | --- |
| `GET  /api/state` | 各域领导者、租约世代、水位、待裁决缓冲、字段统计 |
| `GET  /api/topology` | 跨版本合并后的统一拓扑索引（含裁决依据与换算记录） |
| `POST /api/merge` | 分批归并（`max_concurrency` / `time_budget_ms` 可调） |
| `POST /api/recover` | 崩溃恢复：页对齐校验、半提交批次重放、状态链校验 |
| `GET  /api/verify` | 分批归并与一次性重放的一致性比对 |
| `POST /api/policy/commit` | 提交版本化策略包并计算失效闭包 |
| `GET  /api/policy/status` | 当前策略版本与回调触发日志 |

业务错误统一返回 HTTP 422，携带独立错误码与可诊断字段，例如
`{"error": {"code": "E_VERSION_REGRESS", "fields": {"attempted": 1, "current": 1}}}`。

## 架构

```
coord_workbench/
  errors.py        业务错误码体系（独立 code + 可诊断 fields）
  seedgen.py       固定种子确定性样本生成（含 v2 分片哈希与 SQLite 元数据）
  validation.py    四类输入解析校验（需求 1）
  merge.py         v1/v2 跨版本拓扑合并与统一索引（需求 2）
  batcher.py       逻辑时钟驱动的乱序缓冲与分批归并（需求 3）
  scheduler.py     并发上限 + 时间预算双约束调度器（需求 4）
  statemachine.py  租约世代与领导者指针状态机（需求 5）
  persistence.py   SQLite 账本、版本化策略、文件锁（需求 4/6/7/8）
  invalidation.py  依赖闭包与增量失效裁决（需求 6）
  recovery.py      半提交识别、缺失事件重放、状态链校验（需求 7）
  engine.py        编排：两阶段批次提交、调度、状态报告
  api.py / cli.py  FastAPI 诊断接口与等价命令行（需求 8）
```

关键设计：

- **两阶段提交**：批次先写 `batch_journal(STARTED)`（独立事务），再在
  单个原子事务内写入投票、域状态（水位/租约世代/领导者指针版本/待裁决
  缓冲/已提交序号）与 `COMMITTED`。两阶段之间被 SIGKILL 会留下可识别的
  半提交记录，恢复路径只重放缺失事件，唯一索引保证投票不重复累计。
- **状态链**：每次领导者切换把 `(域, 新世代, 新领导者, 生效时钟)` 链接进
  `leader_history` 的哈希链，恢复时逐节校验，篡改会报 `E_CHAIN_BROKEN`。
- **跨版本裁决**：同一节点以声明逻辑时钟（绝对毫秒）新者优先，时钟相同
  按来源格式优先级 v2 > v1；v1 相对秒在注入逻辑时钟下换算并记录标度
  （1000）与舍入方向（floor）；任一分片哈希不匹配在合并前即终止。
- **多进程一致性**：提交路径由 `fcntl.flock` 文件锁串行化，SQLite 主键与
  部分唯一索引兜底，策略指针只在单事务内整体替换。

## 测试与验证结果

```bash
python3 -m pytest tests/ -q
# 56 passed
```

覆盖：种子确定性、全部校验错误码、跨版本裁决、乱序/回拨时钟、调度器
三类未提交结局、租约单调性、版本回退拒绝、SIGKILL 崩溃恢复（子进程
真实 137 退出）、状态链篡改检测、失效闭包不重复触发、4 进程并发归并
单批次单提交、FastAPI 端点。

本地确定性样本实测（种子 20261127，8 节点 / 18 事件 / 2 拓扑域）：

- `verify`：分批归并与一次性重放逐项一致（`consistent: true`，8 批次）。
- `merge`：8 批次全部 `COMMITTED`；领导者 `domain-alpha=node-c`、
  `domain-beta=node-d`、`domain-default=node-g`；租约世代 2/2/1，无双主。
- 崩溃演练：第 3 批 `os._exit(137)` 后，账本出现 1 条半提交记录；
  `recover` 页对齐校验通过、只重放缺失事件、状态链全部 `ok`；续跑后
  与干净运行状态完全一致（ leaders / 租约世代 / 投票逐项相同）。
- 并发演练：4 进程同时归并同一工作目录，最终 8 批次各提交一次、
  18 条投票各计一次、各域已提交序号互斥且全覆盖。
