# 状态演进与恢复一致性诊断组件

从零构建的可集成 Python 组件，把一次状态演进建模为有向无环计算图：

```
初始状态校验 -> 分块状态变换 -> 跨块归约 -> 状态快照提交 -> 恢复裁决
```

全部输入由固定种子确定性生成，离线运行，不依赖真实集群、外部服务或 GPU。
提供可复现的状态输出、定点/浮点误差账本、任务修订与配置换版的增量失效
闭包，以及中断恢复裁决与并发提交控制。

## 环境准备

- Python 3.10+，安装 numpy（离线：`pip install numpy`）。
- sqlite3 使用 Python 标准库，无额外依赖。

```bash
pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成确定性本地样本（任务清单、状态数组、两代配置快照、三类变更清单）
python3 scripts/generate_samples.py --out data/sample

# 2. 运行端到端示例（三档精度、误差账本、失效传播、快照迁移、中断恢复）
python3 examples/run_demo.py

# 3. 运行全部测试（39 个用例）
python3 -m unittest discover -s tests -t .
```

端到端报告写入 `data/demo_output/demo_report.json`，状态与误差账本分别
写入各运行目录的 `states/` 与 `ledgers/`，检查点写入 `checkpoints/`。

## 目录结构

```
statediag/            组件源码
  ingestion.py        要求1 确定性摄入与初始校验
  transforms.py       要求2 分块变换、补偿求和、跨块归约
  precision.py        要求3 float64/float32/定点 三档精度
  ledger.py           要求4 误差账本、放大比、主导归因
  cache.py            要求5 内容寻址缓存键
  invalidation.py     要求5 三类变更的失效闭包与重算/复用
  snapshots.py        要求6 v1/v2 配置快照、迁移、回退保护
  checkpoints.py      要求7 阶段状态机、检查点、恢复裁决
  concurrency.py      要求8 非阻塞提交门 E_STAGE_BUSY
  consistency.py      要求8 跨模块一致性自检 E_INCONSISTENT
  memory.py           固定内存预算
  clock.py            可注入逻辑时钟
scripts/generate_samples.py  固定种子样本生成
examples/run_demo.py         端到端示例
tests/                       39 个 unittest 用例
docs/ARCHITECTURE.md         技术设计
docs/VERIFICATION.md         实际验证记录
```

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_TOLERANCE_EXCEEDED` | 某级误差超过配置容差 |
| `E_SNAPSHOT_ROLLBACK` | 配置快照版本号或校验和回退 |
| `E_STAGE_BUSY` | 提交并发冲突，后到者立即失败不等待 |
| `E_INCONSISTENT` | 账本无法复算或重算/复用分区不一致 |
| `E_VALIDATION` | 任务摄入或配置校验失败 |
| `E_BUDGET_EXCEEDED` | 突破固定内存预算 |

## 核心特性

- float64 分块串行与整段一次执行逐元素位级一致；三档精度共用同一算子。
- 误差账本覆盖初始校验、分块变换、跨块归约、定点量化四级，记录首个偏差
  步骤、量化边界与误差放大比。
- 检查点带单调版本号、已完成集合与内容校验和；中断恢复后账本与无中断
  运行逐字节一致。
- 两次完整运行的全部账本与样本产物经 SHA-256 比对逐字节一致。

详细设计见 `docs/ARCHITECTURE.md`，实测结果见 `docs/VERIFICATION.md`。
