# 跨任务迁移适配调度组件（transfer-scheduler）

本地、确定性、可集成的跨任务迁移与适配领域组件。无网络、无 GPU、无外部
数据集，所有样本与随机过程由固定种子生成；进程池统一使用 `spawn` 启动，
并行结果与按 `task_id` 升序的串行基线在声明精度（默认 `1e-12`）内一致，
实际验证中差异为 `0.0`（逐字节一致）。

## 环境要求

- Python 3.10+（验证环境：Python 3.10.12）
- NumPy（验证版本 1.26.4）
- pytest（验证版本 8.3.3，仅测试需要）

```bash
pip install -r requirements.txt
```

## 目录结构

```
transfer_scheduler/
  determinism.py   # splitmix64 种子派生、稳定哈希、数组字节指纹
  errors.py        # 9 个结构化业务码与 BusinessError 异常体系
  config.py        # 流水/预算/执行/门限的不可变配置
  data.py          # JSONL 任务清单、域统计量、特征/权重数组生成
  threshold.py     # 严格/容差带门限、次正规数保护、配置校验
  pipeline.py      # 四阶段适配流水（标准化→迁移映射→训练→评估）
  planner.py       # 三约束计划器、裁决路径、降级、并行分组
  ledger.py        # 锁保护的原子预算账本
  snapshots.py     # 版本号+校验和快照、整体替换、回退/损坏识别
  concurrency.py   # 非阻塞提交门（E_STAGE_BUSY）
  executor.py      # spawn 进程池、确定性分块归并、故障注入
  consistency.py   # 五类跨模块一致性自检
  service.py       # 编排门面、执行摘要
  demo.py / app.py # 演示场景与可交互 CLI
tests/             # 36 个 pytest 用例
var/               # 数据/缓存/快照运行目录（自动创建）
docs/              # 需求原文与技术说明
```

## 快速开始

```bash
# 生成确定性任务清单并运行完整工作流
python -m transfer_scheduler.app run

# 预算冲突演示（E_COMPUTE_BUDGET / E_MEMORY_BUDGET / E_SLOT_BUDGET）
python -m transfer_scheduler.app demo-budget

# 并行 worker 异常回传演示（默认同进程；--pool 使用真实 spawn 池）
python -m transfer_scheduler.app demo-failure --pool

# 门限稳定性演示（严格 vs 容差带，50 次重复不翻转）
python -m transfer_scheduler.app demo-threshold

# 交互菜单（任务清单、准入拒绝、账本、分组、分数、错误码、快照恢复）
python -m transfer_scheduler.app
```

## 业务码

| 业务码 | 触发场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 门限/迁移强度/adapter 等配置非法，上下文带字段名与取值 |
| `E_COMPUTE_BUDGET` | 计算预算不足（可先尝试降级） |
| `E_MEMORY_BUDGET` | 驻留内存不足 |
| `E_SLOT_BUDGET` | 并行槽位/排队容量不足 |
| `E_STAGE_BUSY` | 并发提交冲突，后到者立即返回不阻塞 |
| `E_SNAPSHOT_CORRUPT` | 快照结构损坏或校验和不符，带类别与版本号 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本回退覆盖被拒绝 |
| `E_WORKER_FAILURE` | worker 阶段异常，带 task_id 与阶段名 |
| `E_INCONSISTENT` | 跨模块自检不一致，带 category 与双方取值 |

## 确定性保证

- 所有随机数来自 `numpy.random.default_rng(derive_seed(...))`，种子派生为
  纯 Python splitmix64 + blake2b，与 `PYTHONHASHSEED`、系统时间无关；
- 无序集合在参与哈希前一律显式排序，不依赖字典遍历顺序；
- 训练为全批量固定顺序梯度步进，梯度缓冲原地复用；
- 每阶段输出与最终分数均有 blake2b 字节指纹，重复运行逐字节一致；
- 进程池按波次提交、组内 `task_id` 升序、固定 `chunksize`，归并再排序。

## 测试

```bash
python -m pytest tests/ -q
# 36 passed
```

详细设计与验收记录见 `docs/TECHNICAL.md`，需求原文见 `docs/REQUIREMENTS.md`。
